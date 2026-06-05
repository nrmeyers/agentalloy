# Test Plan: Container Setup Experience Improvements

## 1. Unit Tests

### Readiness Endpoint (`health_router.py`)

- [UT-1] Test: Readiness returns "ready" when .bootstrap-complete exists
  - File: `tests/test_health_readiness.py`
  - What to test: Create `.bootstrap-complete` file, call `/readiness`
  - Expected: `{"status": "ready"}`

- [UT-2] Test: Readiness returns "warming_up" when .bootstrap-lock exists
  - File: `tests/test_health_readiness.py`
  - What to test: Create `.bootstrap-lock` file, call `/readiness`
  - Expected: `{"status": "warming_up", "progress": {...}}`

- [UT-3] Test: Readiness returns "ready" when neither lock nor complete exists
  - File: `tests/test_health_readiness.py`
  - What to test: Delete both files, call `/readiness`
  - Expected: `{"status": "ready"}` (no bootstrap started yet)

- [UT-4] Test: Readiness returns "error" for stale lock (>2 hours)
  - File: `tests/test_health_readiness.py`
  - What to test: Create `.bootstrap-lock` with mtime 3 hours ago, call `/readiness`
  - Expected: `{"status": "error", "progress": {"error": "stale_lock"}}`

- [UT-5] Test: Readiness includes progress info when warming_up
  - File: `tests/test_health_readiness.py`
  - What to test: Create lock file, create `.bootstrap-progress` with JSON data, call `/readiness`
  - Expected: `{"status": "warming_up", "progress": {"packs_ingested": [...], "embeddings_done": N}}`

- [UT-6] Test: Readiness handles missing progress file gracefully
  - File: `tests/test_health_readiness.py`
  - What to test: Create lock file, no progress file, call `/readiness`
  - Expected: `{"status": "warming_up", "progress": {}}`

- [UT-7] Test: Readiness handles malformed progress JSON
  - File: `tests/test_health_readiness.py`
  - What to test: Create lock file, write invalid JSON to progress file, call `/readiness`
  - Expected: `{"status": "warming_up", "progress": {}}` (graceful degradation)

- [UT-8] Test: Readiness endpoint returns 200 status code
  - File: `tests/test_health_readiness.py`
  - What to test: Call `/readiness` in all states
  - Expected: HTTP 200 in all cases (ready, warming_up, error)

### Entrypoint Script Generation (`container_runtime.py`)

- [UT-9] Test: Entrypoint script creates lock file at start
  - File: `tests/test_container_runtime_readiness.py`
  - What to test: Call `_build_entrypoint_script()`, verify script contains `touch "$APP_DIR/.bootstrap-lock"`
  - Expected: Script contains lock file creation

- [UT-10] Test: Entrypoint script starts uvicorn before pack ingest
  - File: `tests/test_container_runtime_readiness.py`
  - What to test: Call `_build_entrypoint_script()`, verify uvicorn starts in background BEFORE pack ingest
  - Expected: `uv run uvicorn ... &` appears before `uv run agentalloy install-packs`

- [UT-11] Test: Entrypoint script writes progress atomically
  - File: `tests/test_container_runtime_readiness.py`
  - What to test: Call `_build_entrypoint_script()`, verify atomic write pattern (temp file + mv)
  - Expected: Script writes to `.bootstrap-progress.tmp` then `mv` to `.bootstrap-progress`

- [UT-12] Test: Entrypoint script removes lock and creates complete file on finish
  - File: `tests/test_container_runtime_readiness.py`
  - What to test: Call `_build_entrypoint_script()`, verify cleanup commands
  - Expected: Script contains `rm -f "$APP_DIR/.bootstrap-lock"; touch "$APP_DIR/.bootstrap-complete"`

- [UT-13] Test: Entrypoint script writes checkpoints after each pack ingest
  - File: `tests/test_container_runtime_readiness.py`
  - What to test: Call `_build_entrypoint_script()`, verify checkpoint write after pack ingest
  - Expected: Script appends to `.bootstrap-checkpoints` after each pack

- [UT-14] Test: Entrypoint script checks for stale lock on restart
  - File: `tests/test_container_runtime_readiness.py`
  - What to test: Call `_build_entrypoint_script()`, verify stale lock detection
  - Expected: Script checks mtime of `.bootstrap-lock`, removes if > 2 hours

- [UT-15] Test: Entrypoint script reads checkpoints on restart
  - File: `tests/test_container_runtime_readiness.py`
  - What to test: Call `_build_entrypoint_script()`, verify checkpoint file read
  - Expected: Script reads `.bootstrap-checkpoints` and skips already-ingested packs

- [UT-16] Test: Entrypoint script handles corrupted checkpoint file
  - File: `tests/test_container_runtime_readiness.py`
  - What to test: Call `_build_entrypoint_script()`, verify error handling for corrupt checkpoints
  - Expected: Script treats corrupt checkpoints as "no checkpoints" (starts fresh)

### Wait for Readiness (`container_runtime.py`)

- [UT-17] Test: `_wait_for_readiness()` returns True on "ready"
  - File: `tests/test_container_runtime_readiness.py`
  - What to test: Mock `/readiness` returning `{"status": "ready"}`, call `_wait_for_readiness()`
  - Expected: Returns True immediately

- [UT-18] Test: `_wait_for_readiness()` returns False on "error"
  - File: `tests/test_container_runtime_readiness.py`
  - What to test: Mock `/readiness` returning `{"status": "error"}`, call `_wait_for_readiness()`
  - Expected: Returns False immediately

- [UT-19] Test: `_wait_for_readiness()` continues waiting on "warming_up"
  - File: `tests/test_container_runtime_readiness.py`
  - What to test: Mock `/readiness` returning `{"status": "warming_up"}`, call `_wait_for_readiness()`
  - Expected: Continues polling until timeout or "ready"

- [UT-20] Test: `_wait_for_readiness()` fails on container death
  - File: `tests/test_container_runtime_readiness.py`
  - What to test: Mock `/readiness` returning connection error, call `_wait_for_readiness()`
  - Expected: Returns False immediately with clear error

- [UT-21] Test: `_wait_for_readiness()` uses correct timeout for all-packs
  - File: `tests/test_container_runtime_readiness.py`
  - What to test: Call `_wait_for_readiness(port, timeout=1800)`
  - Expected: Timeout of 1800 seconds

- [UT-22] Test: `_wait_for_readiness()` uses correct timeout for limited packs
  - File: `tests/test_container_runtime_readiness.py`
  - What to test: Call `_wait_for_readiness(port, timeout=300)`
  - Expected: Timeout of 300 seconds

### Get Bootstrap Progress (`container_runtime.py`)

- [UT-23] Test: `_get_bootstrap_progress()` returns parsed JSON from progress file
  - File: `tests/test_container_runtime_readiness.py`
  - What to test: Mock `podman exec` returning valid JSON, call `_get_bootstrap_progress()`
  - Expected: Returns parsed dict with pack names and embed counts

- [UT-24] Test: `_get_bootstrap_progress()` returns empty dict on failure
  - File: `tests/test_container_runtime_readiness.py`
  - What to test: Mock `podman exec` returning error, call `_get_bootstrap_progress()`
  - Expected: Returns `{}`

- [UT-25] Test: `_get_bootstrap_progress()` uses detected runtime binary
  - File: `tests/test_container_runtime_readiness.py`
  - What to test: Call `_get_bootstrap_progress("podman", "agentalloy")`
  - Expected: Uses `podman exec` in the command

### Install-Packs Routing (`install_packs.py`)

- [UT-26] Test: `_run()` routes to container when deployment is container-based
  - File: `tests/test_install_packs_container.py`
  - What to test: Mock state with `deployment: "container"`, call `_run()`, verify container exec
  - Expected: Runs `podman exec agentalloy uv run python -m agentalloy.install install-packs ...`

- [UT-27] Test: `_run()` runs locally when deployment is not container
  - File: `tests/test_install_packs_container.py`
  - What to test: Mock state with `deployment: "native"`, call `_run()`, verify local execution
  - Expected: Runs install-packs locally (existing behavior)

- [UT-28] Test: `_run()` returns error when container is not running
  - File: `tests/test_install_packs_container.py`
  - What to test: Mock `podman exec` failing with "container not running", call `_run()`
  - Expected: Returns error with message "Container is not running. Start it first with `agentalloy setup`"

- [UT-29] Test: Concurrent install-packs protection detects existing lock
  - File: `tests/test_install_packs_container.py`
  - What to test: Mock container with `.install-packs-lock` present, call `_run()`
  - Expected: Returns busy message

- [UT-30] Test: Concurrent install-packs protection handles stale lock
  - File: `tests/test_install_packs_container.py`
  - What to test: Mock container with stale lock (mtime > 30 min), call `_run()`
  - Expected: Proceeds with install (stale lock removed)

### Bootstrap Check (`verify.py`)

- [UT-31] Test: `_check_bootstrap_in_progress()` returns bootstrap_in_progress on "warming_up"
  - File: `tests/test_verify_bootstrap.py`
  - What to test: Mock `/readiness` returning `{"status": "warming_up"}`, call `_check_bootstrap_in_progress()`
  - Expected: Returns dict with `status: "bootstrap_in_progress"` and guidance

- [UT-32] Test: `_check_bootstrap_in_progress()` returns None on "ready"
  - File: `tests/test_verify_bootstrap.py`
  - What to test: Mock `/readiness` returning `{"status": "ready"}`, call `_check_bootstrap_in_progress()`
  - Expected: Returns None (proceed with normal checks)

- [UT-33] Test: `_check_bootstrap_in_progress()` returns None on connection failure
  - File: `tests/test_verify_bootstrap.py`
  - What to test: Mock `/readiness` returning connection error, call `_check_bootstrap_in_progress()`
  - Expected: Returns None (service down, not bootstrap)

- [UT-34] Test: `run_checks()` returns early when bootstrap in progress
  - File: `tests/test_verify_bootstrap.py`
  - What to test: Mock `/readiness` returning `warming_up`, call `run_checks()`
  - Expected: Returns `bootstrap_in_progress` result without running full verify suite

### State Migration (`state.py`)

- [UT-35] Test: `_migrate()` adds bootstrap fields when migrating from v4
  - File: `tests/test_state_migration.py`
  - What to test: Pass v4 state to `_migrate()`, verify new fields added
  - Expected: `bootstrap_started_at`, `bootstrap_completed_at`, `bootstrap_packs_ingested`, etc. all present

- [UT-36] Test: `_migrate()` preserves existing fields during v4 → v5 migration
  - File: `tests/test_state_migration.py`
  - What to test: Pass v4 state with existing fields, call `_migrate()`, verify preserved
  - Expected: All original fields unchanged

- [UT-37] Test: `_migrate()` sets schema_version to 5
  - File: `tests/test_state_migration.py`
  - What to test: Call `_migrate(v4_state, 4)`, check schema_version
  - Expected: `schema_version == 5`

- [UT-38] Test: `_empty_state()` includes all new bootstrap fields
  - File: `tests/test_state_migration.py`
  - What to test: Call `_empty_state()`, verify all bootstrap fields present with defaults
  - Expected: All bootstrap fields present, defaults: None/[]/0

- [UT-39] Test: `load_state()` auto-migrates v4 to v5
  - File: `tests/test_state_migration.py`
  - What to test: Write v4 state to disk, call `load_state()`, verify migration
  - Expected: State has schema_version 5 and all new fields

### Concurrent Install-Packs Lock

- [UT-40] Test: Container-side lock creation before ingest
  - File: `tests/test_install_packs_container.py`
  - What to test: Verify entrypoint script creates `.install-packs-lock` before ingest
  - Expected: Lock file created before `uv run agentalloy install-packs`

- [UT-41] Test: Container-side lock removal after ingest
  - File: `tests/test_install_packs_container.py`
  - What to test: Verify entrypoint script removes `.install-packs-lock` after ingest
  - Expected: Lock file removed after ingest completes

## 2. Integration Tests

- [IT-1] Test: Readiness endpoint reads live filesystem state
  - What to test: Create `.bootstrap-lock` and `.bootstrap-progress` files, call `/readiness`
  - Expected: Returns `warming_up` with progress from the files

- [IT-2] Test: Entrypoint script generates valid bash
  - What to test: Call `_build_entrypoint_script()`, run through `bash -n` (syntax check)
  - Expected: No syntax errors

- [IT-3] Test: Setup flow uses `_wait_for_readiness()` instead of inline polling
  - What to test: Mock `_wait_for_readiness()`, call `_run_container_flow()`, verify it's called
  - Expected: `_wait_for_readiness()` is called with correct timeout

- [IT-4] Test: Progress polling displays updates during setup
  - What to test: Mock `_get_bootstrap_progress()` returning progress data, call `_run_container_flow()`
  - Expected: Progress updates displayed in setup output

- [IT-5] Test: Install-packs routing uses correct runtime binary from state
  - What to test: Mock state with `runtime_binary: "docker"`, call `_run()` in container mode
  - Expected: Uses `docker exec` (not `podman exec`)

- [IT-6] Test: Verify returns bootstrap_in_progress during active bootstrap
  - What to test: Mock `/readiness` returning `warming_up`, call `verify.run()`
  - Expected: Returns `bootstrap_in_progress` with guidance, no full verify suite

- [IT-7] Test: Verify proceeds normally after bootstrap complete
  - What to test: Mock `/readiness` returning `ready`, call `verify.run()`
  - Expected: Runs full verify suite (existing behavior)

- [IT-8] Test: State migration preserves completed_steps and other metadata
  - What to test: Write v4 state with completed_steps, models_pulled, etc., call `load_state()`
  - Expected: All metadata preserved, new bootstrap fields added with defaults

## 3. End-to-End Tests

- [E2E-1] Test: Full container setup with all-packs completes within timeout
  - What to test: Run `agentalloy setup --packs all` in container mode with real podman/docker
  - Expected: Setup completes successfully, `/readiness` returns `ready` after bootstrap

- [E2E-2] Test: Setup shows progress updates during bootstrap
  - What to test: Run `agentalloy setup --packs all`, capture output
  - Expected: Progress updates visible (pack name, embed count, elapsed time)

- [E2E-3] Test: `install-packs` routes to container and installs correctly
  - What to test: Run container setup, then `agentalloy install-packs --packs python --no-restart`
  - Expected: Pack installed inside container, verified via `agentalloy inspect`

- [E2E-4] Test: `verify` during bootstrap returns bootstrap_in_progress
  - What to test: Run container setup, then `agentalloy verify` during active bootstrap
  - Expected: Returns `bootstrap_in_progress` with guidance

- [E2E-5] Test: `verify` after bootstrap completes returns success
  - What to test: Run container setup to completion, then `agentalloy verify`
  - Expected: All checks pass

## 4. Edge Cases

- [EC-1] Test: Stale lock file (>2 hours) returns error from readiness
  - What to test: Create `.bootstrap-lock` with mtime 3 hours ago, call `/readiness`
  - Expected: `{"status": "error", "progress": {"error": "stale_lock"}}`

- [EC-2] Test: Container not running during health check fails immediately
  - What to test: Start container, stop it, call `_wait_for_readiness()`
  - Expected: Fails immediately with clear error about container not running

- [EC-3] Test: Partial bootstrap crash resumes from checkpoint
  - What to test: Run setup, kill container mid-ingest, restart, verify resume
  - Expected: Skips already-ingested packs, continues from last checkpoint

- [EC-4] Test: Corrupted checkpoint file starts fresh
  - What to test: Write invalid JSON to `.bootstrap-checkpoints`, restart container
  - Expected: Starts fresh bootstrap (no crash, no error)

- [EC-5] Test: `install-packs` on stopped container returns clear error
  - What to test: Run container setup, stop container, run `install-packs`
  - Expected: Clear error message "Container is not running"

- [EC-6] Test: `verify` on stopped container returns clear message
  - What to test: Run container setup, stop container, run `verify`
  - Expected: Clear message "Service is not running"

- [EC-7] Test: Mixed pack selection uses shorter timeout
  - What to test: Run `agentalloy setup --packs python,nodejs` (limited packs)
  - Expected: Timeout is 300s (not 1800s)

- [EC-8] Test: Network issue during progress polling falls back to elapsed time
  - What to test: Mock `podman exec` failing during progress polling
  - Expected: Shows elapsed time instead of progress data

- [EC-9] Test: Readiness called immediately after container start (before lock file)
  - What to test: Start container, immediately call `/readiness`
  - Expected: Returns `{"status": "ready"}` (no bootstrap started yet)

- [EC-10] Test: Concurrent `install-packs` calls are serialized
  - What to test: Run two `install-packs` commands simultaneously
  - Expected: Second call waits or returns busy message

- [EC-11] Test: Progress file crash recovery (partial write)
  - What to test: Write partial JSON to `.bootstrap-progress`, read it
  - Expected: Falls back to empty progress dict (graceful degradation)

- [EC-12] Test: Entrypoint script with no packs specified
  - What to test: Call `_build_entrypoint_script("")`, verify script skips pack ingest
  - Expected: Script skips pack ingest step, goes directly to re-embed

- [EC-13] Test: Entrypoint script with empty pack list
  - What to test: Call `_build_entrypoint_script("")`, verify no pack ingest commands
  - Expected: Script contains "No packs specified" message

- [EC-14] Test: Schema version newer than current returns error
  - What to test: Write state with `schema_version: 99`, call `load_state()`
  - Expected: Error message "schema_version 99 is newer than this code supports (5)"

- [EC-15] Test: Host `.env` for container deployment only needs API port
  - What to test: Run container setup, verify host `.env` content
  - Expected: Only `AGENTALLOY_API_URL` with port, no embedder URL
