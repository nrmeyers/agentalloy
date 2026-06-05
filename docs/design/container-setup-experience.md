# Design: Container Setup Experience Improvements

## 1. Overview

This design improves the container deployment bootstrap experience for users who select `--packs all`. Currently, the bootstrap is a 10-30 minute opaque process where:

- The API is completely unavailable during bootstrap (uvicorn starts only after pack ingest completes)
- Progress is only visible in container logs (`embedded X/2949`)
- The health-check timeout (300s) is too short for full pack ingest
- `agentalloy verify` reports misleading errors during active bootstrap
- `agentalloy install-packs` fails with `Table Skill does not exist` because it runs on the host but the corpus lives inside the container
- A crash mid-bootstrap means starting over from scratch

The core idea: **start the API server before bootstrap completes**, expose bootstrap state via a new `/readiness` endpoint, and use that endpoint to drive all bootstrap-aware behavior (health checks, verify, progress display).

This is additive — it does not change existing behavior for non-container deployments, limited pack selections, or day-2 operations.

## 2. Architecture

### 2.1 New Components

```
                    +------------------+
                    |  /readiness EP   |  <-- NEW endpoint in health_router.py
                    |  (bootstrap state)|
                    +--------+---------+
                             |
          +------------------+------------------+
          |                                     |
  +-------v--------+                  +---------v--------+
  |  .bootstrap-   |                  |  .bootstrap-     |
  |  lock          |                  |  progress (JSON) |
  |  (file)        |                  |  (file)          |
  +-------+--------+                  +---------+--------+
          |                                     |
          v                                     v
  +-------+--------+                  +---------v--------+
  | Entrypoint     |                  | /readiness EP     |
  | script (bash)  |                  | reads lock +     |
  | creates/removes|                  | progress files   |
  | lock, writes   |                  +------------------+
  | progress        |
  +----------------+
```

### 2.2 Bootstrap State Mechanism

Two files live inside the container at `/app/`:

| File | Purpose | Created by |
|------|---------|------------|
| `/app/.bootstrap-lock` | Signals bootstrap is in progress | Entrypoint script (at start) |
| `/app/.bootstrap-complete` | Signals bootstrap finished | Entrypoint script (at end) |
| `/app/.bootstrap-progress` | Rich progress data (JSON) | Entrypoint script (periodically) |

Lock file semantics:
- Exists + no complete file = bootstrap in progress
- Complete file exists = bootstrap finished (lock may or may not exist)
- Lock file older than 2 hours = stale (previous bootstrap crashed)

Progress file format (JSON):
```json
{
  "packs_ingested": ["python", "nodejs", "go"],
  "packs_total": 20,
  "embeddings_done": 1500,
  "embeddings_total": 2949,
  "current_pack": "rust",
  "started_at": "2025-01-01T00:00:00Z",
  "updated_at": "2025-01-01T00:05:00Z"
}
```

### 2.3 Modified Bootstrap Sequence

**Current (blocking):**
```
1. Migrations
2. Install packs (10-20 min)
3. Re-embed (5-10 min)
4. Touch .bootstrap-complete
5. Start uvicorn  <-- API unavailable for 15-30 min
```

**New (fast-start):**
```
1. Create .bootstrap-lock
2. Write initial .bootstrap-progress
3. Start uvicorn  <-- API available immediately
4. Run migrations
5. Install packs (while uvicorn serves)
6. Re-embed (while uvicorn serves)
7. Update .bootstrap-progress with final counts
8. Remove .bootstrap-lock
9. Touch .bootstrap-complete
```

### 2.4 Component Interactions

```
User runs: agentalloy setup --deployment container --packs all
    |
    v
simple_setup._run_container_flow()
    |
    +-- _build_image()
    +-- _ensure_volume()
    +-- _generate_entrypoint()  <-- NEW: fast-start entrypoint
    +-- _run_container()
    +-- _wait_for_readiness()   <-- NEW: replaces _wait_for_health()
        |
        +-- polls /readiness every 30s
        +-- shows progress from /readiness or podman exec
        +-- succeeds on "ready", waits on "warming_up", fails on "error"
    |
    v
verify (after readiness returns "ready")

Day-2: User runs: agentalloy verify (during bootstrap)
    |
    v
verify.run_checks()
    |
    +-- _check_bootstrap_in_progress()  <-- NEW
        |
        +-- GET /readiness
        +-- if "warming_up": return bootstrap_in_progress result
        +-- else: run full verify suite
```

## 3. File Changes

### 3.1 Modified Files

#### `src/agentalloy/api/health_router.py`

**What changes:** Add new `/readiness` endpoint.

**Why:** Provides bootstrap state to external consumers (setup, verify, health-check polling).

**Key changes:**
- Add `ReadinessResponse` Pydantic model with `status` (Literal["ready", "warming_up", "error"]) and optional `progress` dict.
- Add `ReadinessChecker` class that checks `.bootstrap-lock` and `.bootstrap-complete` files, reads `.bootstrap-progress` if available.
- Add `GET /readiness` endpoint that returns bootstrap state.
- Lock file age check: if lock exists and is older than 2 hours, return `status: "error"` with `progress: {"error": "stale_lock"}`.
- When lock exists but not complete: read progress file (if present) and return `status: "warming_up"` with progress data.
- When complete file exists: return `status: "ready"` (no progress needed).
- When neither file exists: return `status: "ready"` (no bootstrap was attempted — legacy or non-bootstrap deployment).

**File:** `src/agentalloy/api/health_router.py`

#### `src/agentalloy/install/subcommands/container_runtime.py`

**What changes:** Modify `_build_entrypoint_script()` for fast-start mode; replace `_wait_for_health()` with `_wait_for_readiness()`.

**Why:** Fast-start mode starts uvicorn before pack ingest so the API is available during bootstrap. The readiness-aware health check uses the new `/readiness` endpoint.

**Key changes to `_build_entrypoint_script()`:**
- After `set -e`, create `/app/.bootstrap-lock` with a timestamp: `echo "$(date -Iseconds)" > "$APP_DIR/.bootstrap-lock"`
- Before pack ingest, write initial progress: `echo '{"packs_ingested":[],"embeddings_done":0,"packs_total":N,"embeddings_total":2949,"started_at":"$(date -Iseconds)"}' > "$APP_DIR/.bootstrap-progress"`
- Start uvicorn in the background BEFORE pack ingest: `exec uv run uvicorn ... &` (or start it and continue)
- After each pack ingest, append the pack name to progress: update `.bootstrap-progress` with the new pack name
- During re-embed, periodically update `.bootstrap-progress` with `embeddings_done` count
- On completion: remove lock file (`rm -f "$APP_DIR/.bootstrap-lock"`), touch complete file, write final progress
- Handle early exit: if `.bootstrap-complete` already exists, still create lock then immediately remove it and touch complete (idempotent)

**Key changes to health check function:**
- Replace `_wait_for_health()` with `_wait_for_readiness(port, timeout=1800)`
- Poll `/readiness` instead of `/health`
- Parse JSON response:
  - `status == "ready"`: return True
  - `status == "warming_up"`: continue waiting, optionally show progress
  - `status == "error"`: check for `progress.error == "stale_lock"` — if so, remove stale lock and continue (bootstrap will resume)
  - Container not alive (OSError on HTTP request): return False
- Timeout: 1800s (30 min) for all-pack deployments, 300s for limited pack deployments
- Exponential backoff: start at 5s, double up to 60s max

**New function:** `_get_bootstrap_progress(runtime, container_name)` — reads `/app/.bootstrap-progress` via `podman exec` or `docker exec`, parses JSON, returns dict.

**File:** `src/agentalloy/install/subcommands/container_runtime.py`

#### `src/agentalloy/install/subcommands/simple_setup.py`

**What changes:** Use `_wait_for_readiness()` instead of `_wait_for_health()` in `_run_container_flow()`. Show progress during bootstrap.

**Why:** The setup output should display bootstrap progress instead of being blank for 10-30 minutes.

**Key changes:**
- Replace `_wait_for_health(port)` call with `_wait_for_readiness(port, timeout=1800)`
- During the wait loop, every 30 seconds:
  - Try `_get_bootstrap_progress()` via `podman exec`
  - If successful, parse and display progress: pack name, embeddings done/total, elapsed time
  - Calculate ETA: `(elapsed / embeddings_done) * embeddings_total - elapsed` (if embeddings_done > 0)
  - Display as spinner or progress bar: `[=====>                ] 35% (ETA: 12m)`
  - If progress file unavailable, fall back to showing elapsed time with a spinner
- Determine timeout based on pack selection:
  - If packs == "all" (expanded to full list): use 1800s
  - Otherwise: use 300s
- Pass the timeout to `_wait_for_readiness()`

**File:** `src/agentalloy/install/subcommands/simple_setup.py`

#### `src/agentalloy/install/subcommands/install_packs.py`

**What changes:** Add container routing for `install-packs` command.

**Why:** Running `agentalloy install-packs --packs all --no-restart` on the host fails because the corpus lives inside the container.

**Key changes:**
- In `_run()`, add a check at the start:
  ```python
  from agentalloy.install.container_service import is_in_container
  st = install_state.load_state()
  if not is_in_container() and st.get("deployment") == "container":
      # Route to container
      return _run_container_install_packs(args, st)
  ```
- New function `_run_container_install_packs(args, state)`:
  - Check if container is running: `podman inspect agentalloy` or `docker inspect agentalloy`
  - If not running: return error "Container 'agentalloy' is not running. Start it with `agentalloy setup` first."
  - Execute: `{runtime} exec agentalloy uv run python -m agentalloy.install install-packs --packs {packs} --no-restart`
  - Stream output to user
  - Return the exit code from the container execution
- The container-side execution uses the correct corpus paths (inside the container at `/app/data/`)

**File:** `src/agentalloy/install/subcommands/install_packs.py`

#### `src/agentalloy/install/subcommands/verify.py`

**What changes:** Add bootstrap-in-progress check at the start of `run_checks()`.

**Why:** During active bootstrap, verify reports misleading errors ("port bound by non-agentalloy process", "/diagnostics/runtime unreachable") instead of recognizing that bootstrap is in progress.

**Key changes:**
- New function `_check_bootstrap_in_progress(port)`:
  - Try GET `/readiness` endpoint (with 3s timeout)
  - If returns `status: "warming_up"`: return early result dict with `all_checks_passed: False`, `bootstrap_in_progress: True`, and a friendly message
  - If returns `status: "ready"`: return None (bootstrap complete, proceed with full verify)
  - If returns `status: "error"`: return None (proceed with full verify — stale lock will be handled)
  - If connection fails (container not alive): return early result with "service not running" message
- In `run_checks()`, call `_check_bootstrap_in_progress(port)` first. If it returns a result (not None), return that result immediately without running the full verify suite.

**File:** `src/agentalloy/install/subcommands/verify.py`

#### `src/agentalloy/install/state.py`

**What changes:** Add bootstrap state fields to the install state schema.

**Why:** Track bootstrap progress for resume and diagnostics.

**Key changes:**
- Add new fields to `_empty_state()`:
  ```python
  "bootstrap_started_at": None,     # ISO 8601 timestamp
  "bootstrap_completed_at": None,   # ISO 8601 timestamp
  "bootstrap_packs_ingested": [],   # list of pack names
  "bootstrap_reembed_count": 0,     # number of fragments embedded
  ```
- Schema version stays at 4 (these are nullable additions, backward compatible)
- Update `record_step()` to optionally record bootstrap checkpoints
- No migration needed — new fields are nullable and default to None/empty

**File:** `src/agentalloy/install/state.py`

### 3.2 New Files

None. All changes are to existing files.

### 3.3 Test Files

#### `tests/test_readiness_endpoint.py` (NEW)

Tests for the `/readiness` endpoint.

#### `tests/test_wait_for_readiness.py` (NEW)

Tests for the readiness-aware health check polling.

#### `tests/test_bootstrap_progress.py` (NEW)

Tests for bootstrap progress tracking and display.

#### `tests/test_install_packs_container.py` (NEW)

Tests for container routing of `install-packs`.

#### `tests/test_verify_bootstrap.py` (NEW)

Tests for verify behavior during bootstrap.

## 4. API Changes

### 4.1 New Endpoint

```python
class ReadinessResponse(BaseModel):
    status: Literal["ready", "warming_up", "error"]
    progress: dict[str, Any] | None = None

# Progress dict structure (when status is "warming_up"):
# {
#     "packs_ingested": int,        # Number of packs ingested
#     "packs_total": int,           # Total packs to ingest
#     "embeddings_done": int,       # Number of embeddings done
#     "embeddings_total": int,      # Total embeddings
#     "current_pack": str | None,   # Pack currently being ingested
#     "started_at": str | None,     # ISO 8601 timestamp
#     "updated_at": str | None,     # ISO 8601 timestamp
#     "error": str | None           # Only when status is "error" (e.g., "stale_lock")
# }
```

### 4.2 New Functions

```python
# health_router.py
class ReadinessChecker:
    def __init__(self, app_dir: Path = Path("/app")) -> None: ...
    async def check(self) -> ReadinessResponse:
        """Check bootstrap state.
        
        Returns:
            - status="ready" when .bootstrap-complete exists
            - status="warming_up" with progress when .bootstrap-lock exists
            - status="error" with progress when lock is stale (>2h)
            - status="ready" when neither file exists (no bootstrap attempted)
        """

# container_runtime.py
def _wait_for_readiness(port: int, timeout: int = 1800) -> bool:
    """Poll /readiness with gated logic.
    
    - If /readiness returns ready: return True
    - If /readiness returns warming_up: show progress, continue waiting
    - If /readiness returns error: check for stale_lock, handle accordingly
    - If container is not alive: return False
    """

def _get_bootstrap_progress(runtime: str, container_name: str) -> dict[str, Any] | None:
    """Get bootstrap progress from container state file.
    
    Runs: {runtime} exec {container_name} cat /app/.bootstrap-progress
    Returns parsed JSON dict, or None if file doesn't exist or command fails.
    """

def _run_container_install_packs(args: argparse.Namespace, state: dict) -> int:
    """Route install-packs to the running container.
    
    Checks container is running, then executes:
    {runtime} exec agentalloy uv run python -m agentalloy.install install-packs ...
    
    Returns exit code from container execution.
    """

# verify.py
def _check_bootstrap_in_progress(port: int) -> dict[str, Any] | None:
    """Check if bootstrap is in progress by checking /readiness endpoint.
    
    Returns:
        - dict with bootstrap_in_progress=True if warming_up
        - dict with service_not_running=True if container is down
        - None if bootstrap is complete or not detected
    """
```

### 4.3 Modified Functions

```python
# container_runtime.py
# REMOVED:
def _wait_for_health(port: int, timeout: int = 300) -> bool:

# ADDED (replacement):
def _wait_for_readiness(port: int, timeout: int = 1800) -> bool:

# simple_setup.py
# Modified: _run_container_flow()
# - Uses _wait_for_readiness() instead of _wait_for_health()
# - Shows progress during bootstrap
# - Uses adaptive timeout (1800s for all, 300s for limited)

# install_packs.py
# Modified: _run()
# - Added container routing check at start
# - Routes to _run_container_install_packs() when deployment is container-based
```

## 5. Data Changes

### 5.1 Install State Schema Additions

```python
# Fields added to install-state.json (nullable, no migration needed):
{
    "bootstrap_started_at": "2025-01-01T00:00:00Z",  # When bootstrap started
    "bootstrap_completed_at": "2025-01-01T00:30:00Z",  # When bootstrap finished
    "bootstrap_packs_ingested": ["python", "nodejs", ...],  # List of ingested packs
    "bootstrap_reembed_count": 2949,  # Number of fragments embedded
}
```

### 5.2 Container File System State

Three files live inside the container at `/app/`:

| File | Format | Purpose |
|------|--------|---------|
| `.bootstrap-lock` | Text (ISO 8601 timestamp) | Signals bootstrap in progress |
| `.bootstrap-complete` | Empty file (touch) | Signals bootstrap finished |
| `.bootstrap-progress` | JSON | Rich progress data |

### 5.3 Lock File Stale Detection

The lock file contains a timestamp. Stale detection:
```python
lock_age = time.time() - lock_path.stat().st_mtime
if lock_age > 7200:  # 2 hours
    # Stale lock — previous bootstrap crashed
    return ReadinessResponse(status="error", progress={"error": "stale_lock"})
```

When a stale lock is detected by `/readiness`, `_wait_for_readiness()` removes the stale lock and creates a new one, allowing bootstrap to resume from the last checkpoint.

## 6. Sequence

### 6.1 Main Container Setup Flow (Happy Path)

```
1. User runs: agentalloy setup --deployment container --packs all
2. Shared flow: hardware detection, pack selection, review
3. Runtime detection: podman (preferred) or docker
4. Build context location
5. Preflight checks
6. Build image
7. Ensure volume
8. Generate fast-start entrypoint script
   a. Creates .bootstrap-lock at start
   b. Writes initial .bootstrap-progress JSON
   c. Starts uvicorn in background
   d. Runs migrations, pack ingest, re-embed
   e. On completion: removes lock, touches complete, writes final progress
9. Run container with entrypoint
10. _wait_for_readiness() starts polling /readiness:
    a. First poll: container not alive yet, wait 5s
    b. Second poll: /readiness returns warming_up (uvicorn started, bootstrap in progress)
    c. Every 30s: try to read /app/.bootstrap-progress via podman exec
    d. Display progress: "Packs: 5/20, Embeddings: 1200/2949 (ETA: 8m)"
    e. After 15 min: /readiness returns ready, _wait_for_readiness() returns True
11. Record state to install_state
12. Run verify (bootstrap complete, full verify suite runs)
13. Wire harness (if requested)
14. Display success message
```

### 6.2 Verify During Bootstrap

```
1. User runs: agentalloy verify (during active bootstrap)
2. run_checks() calls _check_bootstrap_in_progress(port)
3. GET /readiness returns {"status": "warming_up", "progress": {...}}
4. _check_bootstrap_in_progress() returns early result:
   {
       "bootstrap_in_progress": true,
       "all_checks_passed": false,
       "checks": [{
           "name": "bootstrap_in_progress",
           "passed": false,
           "error": "Bootstrap in progress — service is warming up",
           "remediation": "Bootstrap is still running. Check progress with 'agentalloy setup' or wait for completion."
       }]
   }
5. verify returns exit code 1 with friendly message
6. User sees: "Bootstrap in progress — service is warming up. Please wait."
```

### 6.3 Install-Packs in Container Deployment

```
1. User runs: agentalloy install-packs --packs python --no-restart
2. _run() checks: is_in_container() = False, state["deployment"] = "container"
3. Routes to _run_container_install_packs(args, state)
4. Checks container is running: podman inspect agentalloy
5. If not running: returns error "Container 'agentalloy' is not running"
6. If running: executes podman exec agentalloy uv run python -m agentalloy.install install-packs --packs python --no-restart
7. Streams output to user
8. Returns exit code from container execution
```

### 6.4 Stale Lock Recovery

```
1. Container crashed during bootstrap (e.g., OOM)
2. .bootstrap-lock exists but is >2 hours old
3. User runs: agentalloy setup --deployment container --packs all
4. Container starts, entrypoint creates new .bootstrap-lock
5. /readiness returns warming_up
6. _wait_for_readiness() polls /readiness:
   a. First poll: /readiness returns warming_up
   b. Progress file shows last checkpoint (e.g., packs_ingested=["python", "nodejs"])
7. Entrypoint script reads checkpoint file, skips already-ingested packs
8. Continues from last checkpoint
9. /readiness returns ready when complete
```

## 7. Error Handling

### 7.1 Stale Lock File

- **Detection:** `/readiness` checks lock file age. If >2 hours, returns `status: "error"` with `progress: {"error": "stale_lock"}`.
- **Recovery:** `_wait_for_readiness()` detects stale lock, removes the lock file, creates a new one, and continues waiting. The entrypoint script's checkpoint logic skips already-ingested packs.
- **User impact:** Transparent — no user-visible error.

### 7.2 Container Not Alive During Health Check

- **Detection:** HTTP request to `/readiness` fails with OSError.
- **Action:** Continue waiting (container may still be starting up).
- **Timeout:** After the configured timeout (1800s or 300s), return False.
- **User impact:** Setup fails with "Service not healthy after Xs". User should check container logs.

### 7.3 Progress File Unavailable

- **Detection:** `podman exec agentalloy cat /app/.bootstrap-progress` fails or returns invalid JSON.
- **Action:** Fall back to showing elapsed time with a spinner. Do not block the wait loop.
- **User impact:** Progress display degrades gracefully — user still sees elapsed time.

### 7.4 Container Not Running During install-packs

- **Detection:** `podman inspect agentalloy` fails.
- **Action:** Return error "Container 'agentalloy' is not running. Start it with `agentalloy setup` first."
- **User impact:** Clear error message with actionable guidance.

### 7.5 Container Not Running During verify

- **Detection:** GET `/readiness` fails with OSError.
- **Action:** Return early result with "service not running" message.
- **User impact:** "Service is not running. Start it with `agentalloy setup` first."

### 7.6 Bootstrap Timeout

- **Detection:** `_wait_for_readiness()` reaches timeout without receiving "ready" status.
- **Action:** Return False. Setup fails.
- **User impact:** "Bootstrap timed out after Xs. Check container logs with `podman logs agentalloy`."

## 8. Performance

### 8.1 /readiness Endpoint

- **Cost:** Minimal — file stat() calls and optional file read. No database queries.
- **Latency:** <1ms per request (filesystem operations).
- **Concurrency:** No locking needed — file reads are atomic on Linux.

### 8.2 Progress Polling

- **Frequency:** Every 30 seconds during setup.
- **Cost:** One `podman exec` call per poll. Each exec is ~50ms.
- **Impact:** Negligible — 30s interval means one exec per 30s, not affecting bootstrap performance.

### 8.3 Entrypoint Script Overhead

- **Uvicorn start delay:** Starting uvicorn before pack ingest adds ~1-2 seconds to entrypoint (uvicorn startup time).
- **Pack ingest:** Unchanged — still runs sequentially inside the container.
- **Re-embed:** Unchanged — still runs sequentially inside the container.
- **Total bootstrap time:** Same as before — the only change is uvicorn starts earlier.

### 8.4 Health Check Polling

- **Frequency:** Every 5-60 seconds (exponential backoff: 5, 10, 20, 30, 45, 60, 60, ...).
- **Cost:** One HTTP request per poll. ~100ms per request.
- **Timeout:** 1800s (30 min) for all-pack, 300s (5 min) for limited packs.

## 9. Security

### 9.1 Entrypoint Script

- The entrypoint script is generated as a temp file and mounted read-only into the container.
- No user input is interpolated into the script — pack names are passed as environment variables.
- The script runs as root inside the container (consistent with current behavior).

### 9.2 /readiness Endpoint

- The endpoint reads files from the container filesystem. No user input is involved.
- No authentication required — the endpoint is only accessible on localhost (consistent with /health).
- No sensitive data exposed — progress info is non-sensitive.

### 9.3 Container Routing for install-packs

- The container routing executes commands inside the running container.
- No new attack surface — the user already has access to the container via `podman exec`.
- The container routing only activates when the deployment state indicates container mode.

### 9.4 Lock File

- Lock files are created/removed by the entrypoint script (runs as root).
- No user-writable paths involved.
- Stale lock detection prevents infinite blocking.

## 10. Testing Strategy

See `docs/tests/container-setup-experience.md` for the full test plan.

Test categories:
- **Unit tests (UT):** Individual functions in isolation (ReadinessChecker, _wait_for_readiness, _check_bootstrap_in_progress, _run_container_install_packs)
- **Integration tests (IT):** Multi-module interactions (readiness endpoint + health check polling, verify + bootstrap check, install-packs + container routing)
- **End-to-end tests (E2E):** Full user flows (setup with --packs all, verify during bootstrap, install-packs in container mode)
- **Edge cases (EC):** Stale lock, container not running, progress file unavailable, mixed pack selection, network issues

## 11. Implementation Phases

### Phase 1: /readiness Endpoint (P0, no dependencies)
- Add `ReadinessResponse` model and `ReadinessChecker` class
- Add `GET /readiness` endpoint to `health_router.py`
- Unit tests for ReadinessChecker (all 4 states)

### Phase 2: Update Entrypoint for Fast-Start (P0, depends on Phase 1)
- Modify `_build_entrypoint_script()` for fast-start mode
- Add lock file creation/removal
- Add progress file writing
- Start uvicorn before pack ingest
- Unit tests for entrypoint script generation

### Phase 3: Replace Health Check with Readiness-Aware Polling (P0, depends on Phase 1)
- Replace `_wait_for_health()` with `_wait_for_readiness()`
- Add stale lock recovery
- Adaptive timeout (1800s for all, 300s for limited)
- Unit tests for _wait_for_readiness()

### Phase 4: Expose Progress in Setup Output (P1, depends on Phase 3)
- Add `_get_bootstrap_progress()` function
- Display progress every 30s during setup
- ETA calculation and display
- Integration tests for progress display

### Phase 5: Container Routing for install-packs (P1, depends on Phase 1)
- Add `_run_container_install_packs()` function
- Add container routing check in `_run()`
- Unit tests for container routing

### Phase 6: Improve Verify During Bootstrap (P1, depends on Phase 1)
- Add `_check_bootstrap_in_progress()` function
- Integrate into `run_checks()`
- Unit tests for bootstrap check

### Phase 7: Resumable Bootstrap Checkpoints (P2, depends on Phase 2)
- Add checkpoint tracking to entrypoint
- Add state fields to install state
- On restart, skip already-ingested packs
- Integration tests for checkpoint resume

### Phase 8: Bootstrap State in Install State (P2, depends on Phase 2)
- Add bootstrap state fields to state.py
- Update state recording in setup flow
- Unit tests for state schema
