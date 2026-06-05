# Design: Container Setup Experience Improvements

## 1. Overview

This design addresses 8 pain points in the container deployment bootstrap flow:
the API being unavailable during 10-30 minute bootstrap, no progress visibility,
health-check timeout too short for full pack ingest, host-side `install-packs`
failing, misleading `verify` errors during bootstrap, no crash recovery
checkpoints, and missing state migration for new fields.

The core idea is a **lock-file + readiness endpoint** pattern: the container
creates `/app/.bootstrap-lock` when bootstrap starts and removes it when
complete. A new `/readiness` endpoint inside the container reports bootstrap
state (`ready`, `warming_up`, `error`). The host-side setup replaces its
inline `/health` polling with a readiness-aware `_wait_for_readiness()` that
understands `warming_up` as "still bootstrapping" (not a failure), supports
longer timeouts (1800s for all-packs), and surfaces periodic progress updates.

The entrypoint script is restructured to start uvicorn **before** pack ingest
(fast-start mode), allowing the API to serve requests during bootstrap. This
is an acceptable trade-off — the `/readiness` endpoint accurately reports
`warming_up` so monitoring and verify can distinguish this state.

## 2. Architecture

```
                    +-------------------+
                    |   Host Side       |
                    +-------------------+
                    | simple_setup.py   |
                    |   _run_container_flow()
                    |     |-- _wait_for_readiness() [NEW]
                    |     |-- progress polling [NEW]
                    |     |-- install-packs routing [NEW]
                    +--------+----------+
                             | HTTP
                             |   /readiness (NEW)
                             |   /health (existing)
                             v
                    +-------------------+
                    | Container         |
                    +-------------------+
                    | health_router.py  |
                    |   GET /readiness [NEW]
                    +--------+----------+
                             |
                             | reads lock/complete/progress files
                             v
                    +-------------------+
                    | File-based state  |
                    +-------------------+
                    | .bootstrap-lock     <- created at bootstrap start
                    | .bootstrap-complete <- created at bootstrap end
                    | .bootstrap-progress <- atomic JSON updates
                    | .bootstrap-checkpoints <- per-pack checkpoints
                    | .install-packs-lock <- concurrent install guard
                    +-------------------+
                    | container_runtime.py
                    |   _build_entrypoint_script() [MODIFIED]
                    |   _wait_for_readiness() [NEW]
                    |   _get_bootstrap_progress() [NEW]
                    +-------------------+
                    | install_packs.py
                    |   _run() [MODIFIED - container routing]
                    +-------------------+
                    | verify.py
                    |   run_checks() [MODIFIED - bootstrap check]
                    +-------------------+
                    | state.py
                    |   _migrate() [MODIFIED - v4->v5]
                    |   _empty_state() [MODIFIED - new fields]
                    +-------------------+
```

### Key Design Decisions

1. **Lock file over process tracking**: We use filesystem-based lock files
   (`.bootstrap-lock`) rather than process tracking because the host and
   container are separate processes. The lock file is the shared state
   mechanism that all components (setup, verify, health check, readiness
   endpoint) agree on.

2. **Fast-start mode (uvicorn before ingest)**: The entrypoint starts uvicorn
   in background before pack ingest. The API serves requests for packs that
   haven't been ingested yet — this is acceptable because `/readiness`
   accurately reports `warming_up` so monitoring tools can distinguish the
   state. Users should not run `install-packs` again during bootstrap.

3. **Atomic progress writes**: The progress file is written atomically (temp
   file + `mv`) to prevent partial JSON on crash. On read failure, the host
   falls back to showing elapsed time.

4. **Checkpoint file over state migration for pack resume**: Checkpoints are
   tracked inside the container (`.bootstrap-checkpoints` file). The entrypoint
   script reads this file on restart to skip already-ingested packs. The host
   side's `install-state.json` gets bootstrap metadata fields (REQ-8) but the
   actual resume logic lives in the container entrypoint.

5. **Runtime binary detection**: All container exec commands use the detected
   runtime binary (`_detect_runtime_binary()` result) rather than hardcoding
   `podman` or `docker`.

## 3. File Changes

### New: `src/agentalloy/api/health_router.py` — Add `/readiness` endpoint

**What changes:** Add a new `GET /readiness` endpoint alongside the existing
`GET /health`. The endpoint reads filesystem state files to determine bootstrap
state.

**Why:** The existing `/health` checks service dependencies but has no awareness
of bootstrap state. Without `/readiness`, setup, verify, and health-check logic
cannot distinguish "service is up but bootstrapping" from "service is fully
ready."

**Key changes:**
- Add `ReadinessResponse` Pydantic model with `status: Literal["ready", "warming_up", "error"]` and optional `progress` dict
- Add `GET /readiness` endpoint function that:
  - Checks `.bootstrap-complete` → returns `ready`
  - Checks `.bootstrap-lock` → returns `warming_up` with progress info
  - Checks for stale lock (>2 hours) → returns `error` with `stale_lock`
  - Neither file exists → returns `ready` (no bootstrap started yet)
- Read progress from `/app/.bootstrap-progress` (JSON, atomic writes)
- Read checkpoint count from `/app/.bootstrap-checkpoints`

### Modified: `src/agentalloy/install/subcommands/container_runtime.py`

**What changes:**
1. **`_build_entrypoint_script()`** — Restructure the entrypoint bash script:
   - Create `.bootstrap-lock` at the start of bootstrap
   - Start uvicorn in background BEFORE pack ingest
   - Write progress to `.bootstrap-progress` using atomic writes (temp + mv)
   - Write checkpoint entries to `.bootstrap-checkpoints` after each pack ingest
   - Remove lock file and create `.bootstrap-complete` when done
   - Check for stale lock on restart (mtime > 2 hours)
   - Check for checkpoint file on restart to skip already-ingested packs

2. **`_wait_for_readiness()`** — NEW function:
   - Poll `/readiness` instead of `/health`
   - Treat `warming_up` as "still bootstrapping" (not failure)
   - Only fail if `/readiness` returns `error` or container is not alive
   - Support configurable timeout (1800s for all-packs, 300s for limited packs)
   - Use detected runtime binary for container exec commands

3. **`_get_bootstrap_progress()`** — NEW function:
   - Execute `{runtime} exec agentalloy cat /app/.bootstrap-progress`
   - Parse JSON progress file (handle malformed JSON gracefully)
   - Return dict with pack names, embedding counts, timestamps

4. **Dead code removal**: The existing `_wait_for_health()` (line 483) is
   dead code — defined but never called. It should be removed or repurposed.

**Why:** The entrypoint is the heart of the bootstrap flow. Restructuring it
to start uvicorn early and create lock/progress files enables fast-start mode,
progress visibility, and crash recovery.

### Modified: `src/agentalloy/install/subcommands/simple_setup.py`

**What changes:**
1. **`_run_container_flow()`** — Replace inline health polling (lines 1308-1326):
   - Call `_wait_for_readiness()` from container_runtime.py instead of inline
     `/health` polling
   - Pass timeout based on pack selection (1800s for all-packs, 300s for limited)
   - During the wait loop, periodically call `_get_bootstrap_progress()` (every
     30 seconds) to display progress in setup output
   - Calculate best-effort ETA from elapsed time vs. progress
   - Fall back to elapsed time display if progress file read fails

2. **Progress display**: Show in setup output:
   - Current pack being ingested
   - Re-embed progress (numerator/denominator)
   - Elapsed time
   - Best-effort ETA

**Why:** The inline health polling loop (lines 1308-1326) only checks `/health`
with a fixed 300s timeout. It has no awareness of bootstrap state, shows no
progress, and times out too early for full pack ingest.

### Modified: `src/agentalloy/install/subcommands/install_packs.py`

**What changes:**
1. **`_run()`** — Add container routing at the start:
   - Check `is_in_container()` from container_service.py
   - Check `install_state.load_state().get("deployment") == "container"`
   - If NOT in container AND deployment is container-based, route to container:
     ```python
     runtime = install_state.load_state().get("runtime_binary", "podman")
     cmd = [runtime, "exec", "agentalloy", "uv", "run", "python", "-m",
            "agentalloy.install", "install-packs", "--packs", packs_str,
            "--no-restart"]
     ```
   - Return exit code and output from container execution
   - If container not running, return clear error message

2. **Concurrent install-packs protection**: Add container-side lock:
   - Create `/app/.install-packs-lock` before starting ingest
   - Check for existing lock before starting (return busy message if present)
   - Remove lock when done
   - Handle stale lock (>30 minutes)

**Why:** Running `agentalloy install-packs` on the host fails with
`Table Skill does not exist` because the corpus lives inside the container.
Routing the command to the container uses the correct corpus paths.

### Modified: `src/agentalloy/install/subcommands/verify.py`

**What changes:**
1. **`run_checks()`** — Add bootstrap check at the start:
   - Before running the full verify suite, try to GET `/readiness` endpoint
   - If it returns `warming_up`, return early with `bootstrap_in_progress`
     status and actionable guidance
   - If the container is not running, return a clear message
   - If it returns `ready`, proceed with normal checks

2. **`_check_bootstrap_in_progress()`** — NEW function:
   - Try to GET `{host}:{port}/readiness`
   - If `warming_up`, return result dict with guidance
   - If `error`, return result dict with error details
   - If connection fails, return None (not bootstrap, service down)
   - If `ready`, return None (bootstrap complete, proceed with checks)

**Why:** During active bootstrap, `verify` reports misleading errors like
"port bound by non-agentalloy process" and `/diagnostics/runtime unreachable`.
The user sees a wall of errors instead of "bootstrap in progress, please wait."

### Modified: `src/agentalloy/install/state.py`

**What changes:**
1. **`CURRENT_SCHEMA_VERSION`** — Bump from 4 to 5

2. **`_empty_state()`** — Add new bootstrap fields:
   ```python
   "bootstrap_started_at": None,
   "bootstrap_completed_at": None,
   "bootstrap_packs_ingested": [],
   "bootstrap_reembed_count": 0,
   "bootstrap_lock_file": "/app/.bootstrap-lock",
   "bootstrap_checkpoints": [],
   ```

3. **`_migrate()`** — Add `from_version < 5` branch:
   - Add all new bootstrap fields with default values
   - Preserve existing fields
   - Bump `schema_version` to 5

**Why:** New bootstrap-related fields are added by this design. Existing state
files (schema v4) need migration to include these fields with sensible defaults.

## 4. API Changes

### New: `GET /readiness` endpoint

```python
class ReadinessResponse(BaseModel):
    status: Literal["ready", "warming_up", "error"]
    progress: dict[str, Any] | None  # Only when status is "warming_up"

# progress fields:
#   packs_ingested: list[str]    # List of pack names that have been ingested
#   embeddings_done: int         # Number of embeddings done
#   bootstrap_complete: bool
```

Response examples:
- `{"status": "ready"}` — bootstrap complete or never started
- `{"status": "warming_up", "progress": {"packs_ingested": ["python", "nodejs"], "embeddings_done": 1500}}`
- `{"status": "error", "progress": {"error": "stale_lock"}}`

### New functions in `container_runtime.py`

```python
def _wait_for_readiness(port: int, timeout: int = 1800) -> bool:
    """Poll /readiness with gated logic:
    - If /readiness returns ready: success
    - If /readiness returns warming_up: show progress, continue waiting
    - If /readiness returns error: fail
    - If container is not alive: fail
    """

def _get_bootstrap_progress(runtime: str, container_name: str) -> dict[str, Any]:
    """Get bootstrap progress from container state file.
    Uses the detected runtime binary (podman/docker) for exec.
    Returns parsed JSON from /app/.bootstrap-progress, or empty dict on failure.
    """
```

### New function in `install_packs.py`

```python
def _run_container_install_packs(packs: list[str], runtime: str, container_name: str) -> int:
    """Route install-packs to the running container instead of running locally."""
```

### New function in `verify.py`

```python
def _check_bootstrap_in_progress(port: int) -> dict[str, Any] | None:
    """Check if bootstrap is in progress by checking /readiness endpoint.
    Returns None if bootstrap is not in progress, or a result dict if it is.
    """
```

## 5. Data Changes

### New state fields in `install-state.json`

```json
{
    "bootstrap_started_at": "2025-01-01T00:00:00Z",
    "bootstrap_completed_at": "2025-01-01T00:30:00Z",
    "bootstrap_packs_ingested": ["python", "nodejs"],
    "bootstrap_reembed_count": 2949,
    "bootstrap_lock_file": "/app/.bootstrap-lock",
    "bootstrap_checkpoints": [
        {"step": "pack_ingested", "pack": "python", "at": "2025-01-01T00:00:00Z"},
        {"step": "reembed_complete", "count": 1500, "at": "2025-01-01T00:05:00Z"}
    ]
}
```

### Container-side state files

| File | Purpose | Format |
|------|---------|--------|
| `.bootstrap-lock` | Lock file — bootstrap in progress | Touch file (mtime = timestamp) |
| `.bootstrap-complete` | Bootstrap completed | Touch file |
| `.bootstrap-progress` | Current progress | JSON (atomic writes) |
| `.bootstrap-checkpoints` | Checkpoint history | JSON lines (one entry per line) |
| `.install-packs-lock` | Concurrent install guard | Touch file |

### Schema migration (v4 → v5)

The `_migrate()` function adds a `from_version < 5` branch that adds all new
bootstrap fields with default values. This is a purely additive migration —
no fields are removed or renamed.

## 6. Sequence

### Main flow: Container setup with fast-start mode

1. **User runs `agentalloy setup --packs all`**
2. **`_run_container_flow()`** detects runtime binary (podman/docker)
3. **Builds container image** via `_build_image()`
4. **Ensures data volume** via `_ensure_volume()`
5. **Generates entrypoint script** via `_generate_entrypoint()`:
   - Script creates `.bootstrap-lock` at start
   - Starts Ollama, pulls embedding model
   - **Starts uvicorn in background** (fast-start)
   - Runs pack ingest (writes progress + checkpoints)
   - Runs re-embed
   - Removes lock, creates `.bootstrap-complete`
6. **Starts container** via `_run_container()`
7. **Polls `/readiness`** via `_wait_for_readiness()`:
   - Every 30 seconds, shows progress (pack name, embed count, ETA)
   - Continues waiting while status is `warming_up`
   - Fails only on `error` or container death
   - Timeout: 1800s for all-packs, 300s for limited packs
8. **Records state** with bootstrap metadata
9. **Writes .env** for host-side operation
10. **Runs verify** — now aware of bootstrap state

### User flow: `install-packs` in container deployment

1. **User runs `agentalloy install-packs --packs python --no-restart`**
2. **`_run()`** checks deployment type from state
3. **If container deployment**: routes command to running container:
   ```
   podman exec agentalloy uv run python -m agentalloy.install install-packs --packs python --no-restart
   ```
4. **Container-side**: checks `.install-packs-lock`, runs ingest, removes lock
5. **Returns** exit code and output to user

### User flow: `verify` during bootstrap

1. **User runs `agentalloy verify`** during active bootstrap
2. **`run_checks()`** calls `_check_bootstrap_in_progress()`
3. **GET `/readiness`** returns `warming_up`
4. **Returns** `bootstrap_in_progress` with guidance:
   "Bootstrap is still in progress. The service is warming up — please wait
   a few more minutes and try again."

## 7. Error Handling

### Stale lock file
- **Detection**: Lock file mtime > 2 hours
- **Readiness endpoint**: Returns `{"status": "error", "progress": {"error": "stale_lock"}}`
- **Entrypoint script**: Detects stale lock, removes it, starts fresh bootstrap
- **User guidance**: "Previous bootstrap appears to have crashed. Starting fresh."

### Container not running during health check
- **Detection**: `/readiness` endpoint unreachable (connection refused)
- **Behavior**: Fail immediately with clear error
- **User guidance**: "Container is not running. Check logs with `podman logs agentalloy`"

### Progress file read failure
- **Detection**: JSON parse error or file not found
- **Behavior**: Fall back to showing elapsed time
- **User guidance**: None needed — fallback is transparent

### Corrupted checkpoint file
- **Detection**: JSON parse error when reading `.bootstrap-checkpoints`
- **Behavior**: Treat as no checkpoints, start fresh
- **User guidance**: None needed — silently starts fresh

### `install-packs` on stopped container
- **Detection**: `podman exec` fails with "container not running"
- **Behavior**: Return clear error message
- **User guidance**: "Container is not running. Start it first with `agentalloy setup`"

### Concurrent `install-packs`
- **Detection**: `.install-packs-lock` exists
- **Behavior**: Return busy message or wait (configurable)
- **User guidance**: "Another install-packs is in progress. Please wait."

### Network issues during progress polling
- **Detection**: `podman exec` fails or progress file not found
- **Behavior**: Fall back to showing elapsed time
- **User guidance**: None needed — fallback is transparent

### Readiness endpoint called before lock file creation
- **Detection**: Neither `.bootstrap-lock` nor `.bootstrap-complete` exists
- **Behavior**: Returns `{"status": "ready"}` (no bootstrap started yet)
- **Rationale**: The entrypoint creates the lock file within seconds of container
  start. A readiness check in this window is acceptable — the service is truly
  ready (no bootstrap needed) or bootstrap is just starting.

## 8. Performance

### Timeout configuration
- **All-packs**: 1800s (30 minutes) — full pack ingest + re-embed can take 15-25 minutes
- **Limited packs**: 300s (5 minutes) — fewer packs, faster ingest
- **Progress polling interval**: 30 seconds — balances responsiveness with overhead

### Atomic writes overhead
- Progress file writes use temp file + `mv` (atomic on same filesystem)
- Overhead: ~1ms per write (negligible compared to pack ingest times)
- Checkpoint writes: one per pack (typically 5-15 packs)

### ETA calculation
- Linear extrapolation: `eta = elapsed / progress * (total - progress)`
- Best-effort only — pack ingest times vary significantly per pack
- No guarantee of accuracy — explicitly documented as such

### Container exec overhead
- Each progress poll spawns a `podman exec` / `docker exec` process
- At 30-second intervals over 30 minutes: ~60 exec calls
- Each exec call: ~50-200ms overhead
- Total overhead: ~3-12 seconds over the entire bootstrap (negligible)

## 9. Security

### No new network attack surface
- The `/readiness` endpoint only reads local filesystem files
- No authentication required (same as `/health`)
- No network-facing changes beyond the existing `/health` endpoint

### Atomic writes prevent race conditions
- Progress file uses temp file + `mv` pattern (same as state file atomic writes)
- Prevents partial JSON from being read during crash
- Prevents TOCTOU races between writer and reader

### Path validation
- Lock file paths are hardcoded (`/app/.bootstrap-lock`, `/app/.bootstrap-complete`)
- No user-controlled paths in the readiness endpoint
- No command injection risk (progress file content is not executed)

### Container exec commands
- Runtime binary is detected once at setup and stored in state
- `podman exec` / `docker exec` commands use fixed container name (`agentalloy`)
- No user input in exec command construction

## 10. Testing Strategy

See `docs/tests/container-setup-improvements.md` for the complete test plan.

### Test plan summary
- **41 unit tests** — readiness endpoint (8), entrypoint generation (8),
  wait_for_readiness (6), bootstrap progress (2), install-packs routing (5),
  bootstrap check (4), state migration (5), concurrent install-packs lock (2)
- **8 integration tests** — filesystem+endpoint, script syntax, setup flow,
  progress display, runtime binary routing, verify bootstrap, verify normal,
  state migration preservation
- **5 end-to-end tests** — full container setup, progress display,
  install-packs routing, verify during bootstrap, verify after bootstrap
- **15 edge cases** — stale locks, container death, checkpoint resume,
  corrupt checkpoints, stopped container, network failure, mixed packs,
  missing lock file, concurrent installs, partial writes, no packs,
  schema version mismatch, host .env minimalism

### Test categories
1. **Unit tests** — Individual functions: readiness endpoint, wait_for_readiness,
   get_bootstrap_progress, install-packs routing, bootstrap check, state migration
2. **Integration tests** — Multi-module interactions: entrypoint script + readiness,
   setup flow + progress polling, verify + bootstrap detection
3. **End-to-end tests** — Full user flows: container setup with all-packs,
   install-packs in container deployment, verify during bootstrap
4. **Edge cases** — Stale locks, corrupted checkpoints, network failures,
   concurrent installs, mixed pack selection

### TDD instructions
- Write tests BEFORE implementation for each function
- Unit tests mock external dependencies (filesystem, network, subprocess)
- Integration tests mock only the outermost boundary (container runtime)
- E2E tests use real container runtime (podman/docker) with mocked network

## 11. Implementation Phases

### Phase 1: Foundation (P0)
- **Task 1**: Add state migration (v4 → v5) — bump schema, add new fields
- **Task 2**: Add `/readiness` endpoint to `health_router.py`
- **Task 3**: Modify entrypoint script for fast-start mode (lock file, early uvicorn)

### Phase 2: Readiness-aware polling (P0)
- **Task 4**: Add `_wait_for_readiness()` to `container_runtime.py`
- **Task 5**: Replace inline health polling in `simple_setup.py`
- **Task 6**: Add `_get_bootstrap_progress()` to `container_runtime.py`

### Phase 3: Progress visibility (P1)
- **Task 7**: Expose bootstrap progress in setup output (simple_setup.py)
- **Task 8**: Add ETA calculation and display

### Phase 4: Container routing (P1)
- **Task 9**: Route `install-packs` to container in `install_packs.py`
- **Task 10**: Add concurrent install-packs protection

### Phase 5: Verify improvements (P1)
- **Task 11**: Add bootstrap check to `verify.py`

### Phase 6: Checkpoint recovery (P2)
- **Task 12**: Add checkpoint tracking to entrypoint script
- **Task 13**: Handle corrupted checkpoint files
