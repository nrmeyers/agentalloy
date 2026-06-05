# Test Plan: Container Setup Experience Improvements

## 1. Unit Tests

### Readiness Endpoint

- [UT-1] Test: ReadinessResponse model validation
  - File: `tests/test_readiness_endpoint.py`
  - What to test: Valid and invalid status values, optional progress field
  - Expected: Valid statuses ("ready", "warming_up", "error") accepted; invalid values raise ValidationError

- [UT-2] Test: ReadinessChecker returns "ready" when .bootstrap-complete exists
  - File: `tests/test_readiness_endpoint.py`
  - What to test: Create temp dir with `.bootstrap-complete` file, run ReadinessChecker.check()
  - Expected: Returns ReadinessResponse(status="ready", progress=None)

- [UT-3] Test: ReadinessChecker returns "warming_up" when .bootstrap-lock exists (not stale)
  - File: `tests/test_readiness_endpoint.py`
  - What to test: Create temp dir with `.bootstrap-lock` (modified < 2h ago) and `.bootstrap-progress`
  - Expected: Returns ReadinessResponse(status="warming_up", progress={...}) with data from progress file

- [UT-4] Test: ReadinessChecker returns "error" with stale_lock when lock > 2h old
  - File: `tests/test_readiness_endpoint.py`
  - What to test: Create temp dir with `.bootstrap-lock` (modified 3h ago), no complete file
  - Expected: Returns ReadinessResponse(status="error", progress={"error": "stale_lock"})

- [UT-5] Test: ReadinessChecker returns "ready" when neither lock nor complete exists
  - File: `tests/test_readiness_endpoint.py`
  - What to test: Empty temp dir, no bootstrap files
  - Expected: Returns ReadinessResponse(status="ready", progress=None)

- [UT-6] Test: ReadinessChecker returns "warming_up" with partial progress when progress file missing
  - File: `tests/test_readiness_endpoint.py`
  - What to test: Create temp dir with `.bootstrap-lock` (not stale), no `.bootstrap-progress`
  - Expected: Returns ReadinessResponse(status="warming_up", progress={"error": "progress_file_missing"})

- [UT-7] Test: ReadinessChecker handles invalid JSON in progress file
  - File: `tests/test_readiness_endpoint.py`
  - What to test: Create temp dir with `.bootstrap-lock` and `.bootstrap-progress` containing "not json"
  - Expected: Returns ReadinessResponse(status="warming_up", progress={"error": "invalid_progress_data"})

### Entrypoint Script Generation

- [UT-8] Test: Entrypoint script creates .bootstrap-lock at start
  - File: `tests/test_container_runtime.py`
  - What to test: Call _build_entrypoint_script(packs="python,nodejs"), check generated script
  - Expected: Script contains `echo "$(date -Iseconds)" > "$APP_DIR/.bootstrap-lock"` before pack ingest

- [UT-9] Test: Entrypoint script starts uvicorn before pack ingest
  - File: `tests/test_container_runtime.py`
  - What to test: Call _build_entrypoint_script(packs="python"), check line order
  - Expected: Uvicorn start line appears BEFORE pack ingest line (uvicorn starts in background)

- [UT-10] Test: Entrypoint script writes progress file on start
  - File: `tests/test_container_runtime.py`
  - What to test: Call _build_entrypoint_script(packs="python,nodejs"), check generated script
  - Expected: Script writes JSON to `.bootstrap-progress` with packs_total=2, embeddings_total=2949

- [UT-11] Test: Entrypoint script removes lock and creates complete on finish
  - File: `tests/test_container_runtime.py`
  - What to test: Call _build_entrypoint_script(packs="python"), check end of script
  - Expected: Script contains `rm -f "$APP_DIR/.bootstrap-lock"` and `touch "$APP_DIR/.bootstrap-complete"`

- [UT-12] Test: Entrypoint script handles already-complete bootstrap
  - File: `tests/test_container_runtime.py`
  - What to test: Call _build_entrypoint_script(packs=""), check early-exit branch
  - Expected: Script checks for `.bootstrap-complete`, creates lock, removes lock, touches complete

- [UT-13] Test: Entrypoint script writes progress after each pack ingest
  - File: `tests/test_container_runtime.py`
  - What to test: Call _build_entrypoint_script(packs="python,nodejs,go"), check script
  - Expected: Script updates `.bootstrap-progress` with each pack name appended to packs_ingested array

### Health Check Polling

- [UT-14] Test: _wait_for_readiness returns True on "ready" response
  - File: `tests/test_wait_for_readiness.py`
  - What to test: Mock HTTP server returning {"status": "ready"}, call _wait_for_readiness(port, timeout=5)
  - Expected: Returns True on first poll

- [UT-15] Test: _wait_for_readiness returns True on "warming_up" then "ready"
  - File: `tests/test_wait_for_readiness.py`
  - What to test: Mock server returns warming_up first, then ready after 2 polls
  - Expected: Returns True after second poll

- [UT-16] Test: _wait_for_readiness returns False on timeout
  - File: `tests/test_wait_for_readiness.py`
  - What to test: Mock server always returns warming_up, timeout=3s
  - Expected: Returns False after timeout

- [UT-17] Test: _wait_for_readiness handles stale lock recovery
  - File: `tests/test_wait_for_readiness.py`
  - What to test: Mock server returns error with stale_lock, then returns warming_up, then ready
  - Expected: Removes stale lock, continues waiting, returns True

- [UT-18] Test: _wait_for_readiness returns False when container not alive
  - File: `tests/test_wait_for_readiness.py`
  - What to test: Mock server raises ConnectionRefusedError, timeout=5s
  - Expected: Returns False after timeout

- [UT-19] Test: _wait_for_readiness uses exponential backoff
  - File: `tests/test_wait_for_readiness.py`
  - What to test: Mock server always raises ConnectionRefusedError, measure time between polls
  - Expected: Intervals double: 5s, 10s, 20s, 30s, 45s, 60s, ...

- [UT-20] Test: _wait_for_readiness uses correct timeout for all vs limited packs
  - File: `tests/test_wait_for_readiness.py`
  - What to test: Call with timeout=1800 (all packs) and timeout=300 (limited packs)
  - Expected: Uses the passed timeout value

### Bootstrap Progress

- [UT-21] Test: _get_bootstrap_progress returns parsed JSON from container
  - File: `tests/test_bootstrap_progress.py`
  - What to test: Mock subprocess.run to return valid JSON from `podman exec`, call _get_bootstrap_progress()
  - Expected: Returns dict with packs_ingested, embeddings_done, etc.

- [UT-22] Test: _get_bootstrap_progress returns None when progress file missing
  - File: `tests/test_bootstrap_progress.py`
  - What to test: Mock subprocess.run to return non-zero exit code
  - Expected: Returns None

- [UT-23] Test: _get_bootstrap_progress returns None when container not running
  - File: `tests/test_bootstrap_progress.py`
  - What to test: Mock subprocess.run to raise CalledProcessError
  - Expected: Returns None

- [UT-24] Test: ETA calculation is correct
  - File: `tests/test_bootstrap_progress.py`
  - What to test: embeddings_done=1000, embeddings_total=2000, elapsed=300s
  - Expected: ETA = (300/1000)*2000 - 300 = 300s

- [UT-25] Test: ETA returns None when embeddings_done is 0
  - File: `tests/test_bootstrap_progress.py`
  - What to test: embeddings_done=0
  - Expected: Returns None (cannot calculate ETA from 0 progress)

### Install-Packs Container Routing

- [UT-26] Test: _run_container_install_packs routes to container when deployment is container
  - File: `tests/test_install_packs_container.py`
  - What to test: Mock is_in_container()=False, state["deployment"]="container", call _run()
  - Expected: Calls _run_container_install_packs()

- [UT-27] Test: _run_container_install_packs returns error when container not running
  - File: `tests/test_install_packs_container.py`
  - What to test: Mock podman inspect to fail
  - Expected: Returns error message "Container 'agentalloy' is not running"

- [UT-28] Test: _run_container_install_packs executes inside container
  - File: `tests/test_install_packs_container.py`
  - What to test: Mock podman inspect to succeed, call _run_container_install_packs()
  - Expected: Runs `podman exec agentalloy uv run python -m agentalloy.install install-packs --packs python --no-restart`

- [UT-29] Test: _run does NOT route to container when already in container
  - File: `tests/test_install_packs_container.py`
  - What to test: Mock is_in_container()=True, state["deployment"]="container"
  - Expected: Does NOT call _run_container_install_packs(), runs normally

- [UT-30] Test: _run does NOT route to container when deployment is native
  - File: `tests/test_install_packs_container.py`
  - What to test: Mock is_in_container()=False, state["deployment"]="native"
  - Expected: Does NOT call _run_container_install_packs(), runs normally

### Verify Bootstrap Check

- [UT-31] Test: _check_bootstrap_in_progress returns bootstrap_in_progress when warming_up
  - File: `tests/test_verify_bootstrap.py`
  - What to test: Mock GET /readiness returns {"status": "warming_up", "progress": {...}}
  - Expected: Returns dict with bootstrap_in_progress=True, all_checks_passed=False

- [UT-32] Test: _check_bootstrap_in_progress returns None when ready
  - File: `tests/test_verify_bootstrap.py`
  - What to test: Mock GET /readiness returns {"status": "ready"}
  - Expected: Returns None (proceed with full verify)

- [UT-33] Test: _check_bootstrap_in_progress returns service_not_running when container down
  - File: `tests/test_verify_bootstrap.py`
  - What to test: Mock GET /readiness raises ConnectionRefusedError
  - Expected: Returns dict with service_not_running=True

- [UT-34] Test: _check_bootstrap_in_progress returns None on stale_lock error
  - File: `tests/test_verify_bootstrap.py`
  - What to test: Mock GET /readiness returns {"status": "error", "progress": {"error": "stale_lock"}}
  - Expected: Returns None (proceed with full verify — stale lock will be handled)

- [UT-35] Test: run_checks returns early when bootstrap in progress
  - File: `tests/test_verify_bootstrap.py`
  - What to test: Mock _check_bootstrap_in_progress to return bootstrap_in_progress result
  - Expected: run_checks returns the bootstrap_in_progress result without running full verify suite

- [UT-36] Test: run_checks runs full verify when bootstrap complete
  - File: `tests/test_verify_bootstrap.py`
  - What to test: Mock _check_bootstrap_in_progress to return None
  - Expected: run_checks proceeds with full verify suite

### State Schema

- [UT-37] Test: _empty_state includes bootstrap fields
  - File: `tests/test_state_bootstrap.py`
  - What to test: Call _empty_state(), check for new fields
  - Expected: State includes bootstrap_started_at=None, bootstrap_completed_at=None, bootstrap_packs_ingested=[], bootstrap_reembed_count=0

- [UT-38] Test: Bootstrap state fields are backward compatible (no migration needed)
  - File: `tests/test_state_bootstrap.py`
  - What to test: Load state file without bootstrap fields, call load_state()
  - Expected: Returns state with bootstrap fields defaulting to None/empty

## 2. Integration Tests

- [IT-1] Test: /readiness endpoint returns correct state with mock files
  - What to test: Create temp dir with mock files, mount as volume, start test server, GET /readiness
  - Expected: Endpoint returns correct status for each file combination

- [IT-2] Test: Fast-start entrypoint starts uvicorn and bootstrap in parallel
  - What to test: Run entrypoint script in container, poll /health while bootstrap runs
  - Expected: /health returns 200 before bootstrap completes (within 10s)

- [IT-3] Test: _wait_for_readiness correctly polls /readiness and handles state transitions
  - What to test: Mock /readiness to transition: not-alive -> warming_up -> ready
  - Expected: _wait_for_readiness returns True after transition to ready

- [IT-4] Test: Setup output shows progress during bootstrap
  - What to test: Mock _get_bootstrap_progress to return increasing progress values
  - Expected: Setup output displays progress updates every 30s with pack name, embed count, ETA

- [IT-5] Test: Verify returns bootstrap_in_progress during active bootstrap
  - What to test: Mock /readiness to return warming_up, run verify
  - Expected: Verify returns exit code 1 with bootstrap_in_progress message

- [IT-6] Test: Install-packs routes to container in container deployment mode
  - What to test: Mock is_in_container()=False, state["deployment"]="container", run install-packs
  - Expected: Command executes inside container, not on host

- [IT-7] Test: Stale lock recovery works end-to-end
  - What to test: Create stale lock file (>2h old), start container, poll /readiness
  - Expected: /readiness returns error with stale_lock, _wait_for_readiness removes stale lock and continues

- [IT-8] Test: Bootstrap state is recorded in install-state.json
  - What to test: Run setup flow with mocked container, check state file after completion
  - Expected: State file includes bootstrap_started_at, bootstrap_completed_at, bootstrap_packs_ingested, bootstrap_reembed_count

## 3. End-to-End Tests

- [E2E-1] Test: Full container setup with --packs all completes successfully
  - What to test: Run `agentalloy setup --deployment container --packs all --non-interactive --yes` with a real container runtime (podman)
  - Expected: Setup completes, /readiness returns ready, verify passes

- [E2E-2] Test: Setup shows progress during bootstrap
  - What to test: Run setup with --packs all, capture output
  - Expected: Output shows progress updates (pack names, embed counts, ETA) every ~30s

- [E2E-3] Test: Verify during bootstrap returns bootstrap_in_progress
  - What to test: Run setup with --packs all (background), run verify (foreground)
  - Expected: Verify returns bootstrap_in_progress message, does not run full verify suite

- [E2E-4] Test: Install-packs routes to container in container deployment
  - What to test: Run setup --deployment container --packs all, then install-packs --packs python --no-restart
  - Expected: Pack is installed inside container, not on host

- [E2E-5] Test: Stale lock recovery — restart mid-bootstrap
  - What to test: Run setup, stop container mid-bootstrap, restart setup
  - Expected: Bootstrap resumes from last checkpoint, already-ingested packs are skipped

## 4. Edge Cases

- [EC-1] Test: Progress file returns invalid JSON
  - What to test: Mock podman exec to return "not valid json"
  - Expected: Progress display falls back to showing elapsed time, does not crash

- [EC-2] Test: Container dies during health check polling
  - What to test: Mock /readiness to fail after 3 successful polls
  - Expected: _wait_for_readiness returns False, setup reports container failure

- [EC-3] Test: Mixed pack selection (not "all") uses shorter timeout
  - What to test: Run setup with --packs python,nodejs (limited packs)
  - Expected: Timeout is 300s, not 1800s

- [EC-4] Test: Entrypoint script handles SIGTERM during bootstrap
  - What to test: Send SIGTERM to container during pack ingest
  - Expected: uvicorn shuts down gracefully, lock file is cleaned up

- [EC-5] Test: Multiple concurrent verify calls during bootstrap
  - What to test: Run verify 5 times concurrently during active bootstrap
  - Expected: All return bootstrap_in_progress, no errors or race conditions

- [EC-6] Test: /readiness endpoint handles concurrent requests
  - What to test: Send 100 concurrent GET /readiness requests
  - Expected: All return correct status, no errors

- [EC-7] Test: Progress file is not overwritten during update
  - What to test: Entrypoint script updates progress file while /readiness reads it
  - Expected: No partial reads — file writes are atomic (write to temp, then rename)

- [EC-8] Test: Container with no network during bootstrap
  - What to test: Simulate network failure during bootstrap
  - Expected: /readiness still works (reads local files), health check continues polling

- [EC-9] Test: Entrypoint script with empty pack list
  - What to test: Run setup with --packs "" (empty)
  - Expected: No pack ingest, uvicorn starts immediately, bootstrap completes in seconds

- [EC-10] Test: Install-packs on stopped container returns clear error
  - What to test: Run install-packs when container is stopped (not running)
  - Expected: Returns error "Container 'agentalloy' is not running. Start it with agentalloy setup first."

- [EC-11] Test: Verify on stopped container returns clear error
  - What to test: Run verify when container is stopped
  - Expected: Returns "Service is not running. Start it with agentalloy setup first."

- [EC-12] Test: Lock file with no timestamp content
  - What to test: Create .bootstrap-lock with empty content (not a valid timestamp)
  - Expected: /readiness returns error (cannot parse timestamp as older than 2h)

- [EC-13] Test: Entrypoint script with special characters in pack names
  - What to test: Run setup with pack names containing spaces or special chars
  - Expected: Pack names are properly shell-quoted in the generated script

- [EC-14] Test: Progress display with very long ETA
  - What to test: embeddings_done=1, embeddings_total=2949, elapsed=10s
  - Expected: ETA is displayed as "unknown" or "N/A" instead of a misleading number

- [EC-15] Test: Entrypoint script cleanup on abnormal exit
  - What to test: Kill entrypoint script mid-bootstrap (kill -9)
  - Expected: Lock file remains (stale lock), next bootstrap detects and recovers from it
