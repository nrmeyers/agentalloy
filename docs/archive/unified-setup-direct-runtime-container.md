# OBSOLETE — Pre-migration

This document describes the pre-GHCR-migration container setup (build-based approach).
It has been superseded by the GHCR pull-based flow. See:
- `design/container-ghcr-migration.md` — design doc
- `specs/container-ghcr-migration.md` — spec
- `plans/container-ghcr-migration-plan.md` — implementation plan

# Test Plan: Unified Setup UX with Direct Runtime Container Execution

## Unit Tests

### UT-1: SetupConfig Field Changes
- File: `tests/test_simple_setup.py::TestSetupConfig`
- What to test:
  - `SetupConfig` has new fields: `runtime_binary`, `image_tag`, `container_name`, `data_volume`
  - `SetupConfig` no longer has `compose_binary`, `compose_file`
  - Default values are correct
- Expected: All new fields present with correct defaults; old fields removed

### UT-2: Runtime Binary Detection
- File: `tests/test_simple_setup.py::TestRuntimeDetection`
- What to test:
  - `_detect_runtime_binary()` returns "podman" when only podman available
  - `_detect_runtime_binary()` returns "docker" when only docker available
  - `_detect_runtime_binary()` returns "podman" when both available (podman preferred)
  - `_detect_runtime_binary()` returns None when neither available
- Expected: Correct binary detected in all scenarios

### UT-3: Build Context Location
- File: `tests/test_simple_setup.py::TestBuildContext`
- What to test:
  - `_locate_build_context()` finds build assets in cwd
  - `_locate_build_context()` finds build assets in parents[4]
  - `_locate_build_context()` returns None when assets not found locally
- Expected: Correct path returned or None

### UT-4: Build Image Command
- File: `tests/test_simple_setup.py::TestBuildImage`
- What to test:
  - `_build_image()` runs correct subprocess command
  - `_build_image()` returns 0 on success
  - `_build_image()` returns non-zero on failure
- Expected: Correct command with correct args, return code propagated

### UT-5: Volume Creation
- File: `tests/test_simple_setup.py::TestVolumeCreation`
- What to test:
  - `_create_volume()` runs correct subprocess command
  - `_create_volume()` returns 0 on success
  - `_create_volume()` returns non-zero on failure
- Expected: `podman volume create agentalloy-data` executed correctly

### UT-6: Container Run
- File: `tests/test_simple_setup.py::TestContainerRun`
- What to test:
  - `_run_container()` runs correct subprocess command with all flags
  - Port mapping: `-p {port}:47950`
  - Volume mount: `-v agentalloy-data:/app/data`
  - Environment: `-e AGENTALLOY_PACKS={packs}`
  - Container name: `--name agentalloy`
  - Replace flag: `--replace`
  - Detached: `-d`
- Expected: All flags present in subprocess call

### UT-7: Health Check
- File: `tests/test_simple_setup.py::TestHealthCheck`
- What to test:
  - `_wait_for_health()` returns True when /health returns 200
  - `_wait_for_health()` returns False when /health times out (300s)
  - `_wait_for_health()` uses correct backoff interval (2s)
  - `_wait_for_health()` polls correct URL: http://localhost:{port}/health
- Expected: Correct behavior on success and timeout

### UT-8: State Recording
- File: `tests/test_simple_setup.py::TestStateRecording`
- What to test:
  - `_record_container_state()` saves all new fields to install state
  - State keys: `runtime_binary`, `image_tag`, `container_name`, `data_volume`, `port`
  - `deployment` set to "container"
  - State file is valid JSON
- Expected: All fields persisted correctly

### UT-9: Preflight Runtime Check
- File: `tests/test_preflight.py::TestRuntimeCheck`
- What to test:
  - `_check_runtime_binary()` passes when podman available
  - `_check_runtime_binary()` passes when docker available
  - `_check_runtime_binary()` fails when neither available
  - Failure message mentions both podman and docker
- Expected: Correct pass/fail based on PATH

### UT-10: Preflight Build Context Check
- File: `tests/test_preflight.py::TestBuildContextCheck`
- What to test:
  - `_check_build_context_present()` passes when Containerfile, pyproject.toml, uv.lock exist
  - `_check_build_context_present()` fails when any asset missing
  - Failure message lists missing files
- Expected: Correct pass/fail based on file existence

### UT-11: State Migration
- File: `tests/test_state.py::TestStateMigration`
- What to test:
  - `load_state()` with old `compose_binary` key: logs warning, ignores key
  - `load_state()` with old `compose_file` key: logs warning, ignores key
  - `load_state()` with new keys: works normally
  - `load_state()` with both old and new keys: new keys used, old ignored
- Expected: Graceful migration, no errors

## Integration Tests

### IT-1: Full Container Flow
- File: `tests/test_simple_setup.py::TestContainerFlow`
- What to test:
  - `run_setup()` with `deployment="container"` completes end-to-end
  - All subprocess calls made in correct order (build → volume → run → health)
  - State recorded after successful setup
  - Return code 0 on success
- Expected: Full container flow succeeds with correct state

### IT-2: Container Flow with Runtime Missing
- File: `tests/test_simple_setup.py::TestContainerFlow`
- What to test:
  - `run_setup()` with `deployment="container"` when neither podman nor docker available
  - Returns exit code 1
  - Error message mentions podman and docker
- Expected: Clean failure with helpful error

### IT-3: Container Flow with Build Context Missing
- File: `tests/test_simple_setup.py::TestContainerFlow`
- What to test:
  - `run_setup()` with `deployment="container"` when no build context found
  - Returns exit code 1
  - Error message mentions build context
- Expected: Clean failure with helpful error

### IT-4: Container Flow with Build Failure
- File: `tests/test_simple_setup.py::TestContainerFlow`
- What to test:
  - `run_setup()` with `deployment="container"` when build command fails
  - Returns exit code 1
  - Build output included in error message
- Expected: Clean failure with build output

### IT-5: Container Flow with Health Check Timeout
- File: `tests/test_simple_setup.py::TestContainerFlow`
- What to test:
  - `run_setup()` with `deployment="container"` when /health never returns 200
  - Returns exit code 1
  - Error message suggests checking container logs
- Expected: Clean failure with troubleshooting hint

### IT-6: Preflight Container Phase
- File: `tests/test_preflight.py::TestContainerPhase`
- What to test:
  - `run_preflight(phase="container")` passes when runtime and build context available
  - `run_preflight(phase="container")` fails when runtime missing
  - `run_preflight(phase="container")` fails when build context missing
  - `run_preflight(phase="container")` fails when port in use
- Expected: Correct pass/fail for all preflight checks

### IT-7: Native Path Unchanged
- File: `tests/test_simple_setup.py::TestNativeFlow`
- What to test:
  - `run_setup()` with `deployment="native"` still works
  - Native prompts still executed
  - Native flow still calls pull_models → start_embed_server → install_packs → wire → verify
  - No regression in native path
- Expected: Native path fully functional, no changes

### IT-8: Packs Prompt Shared
- File: `tests/test_simple_setup.py::TestSharedDiscovery`
- What to test:
  - `run_setup()` with `deployment="container"` still prompts for packs
  - Packs value passed to container via AGENTALLOY_PACKS env var
  - Packs selection affects container setup
- Expected: Packs prompt runs for both paths

## End-to-End Tests

### E2E-1: Interactive Container Setup
- File: `tests/test_simple_setup.py::TestE2E`
- What to test:
  - Full interactive flow: detect → deploy choice → packs → review → confirm → container setup
  - All prompts displayed correctly
  - User can cancel before confirmation
  - Confirmation triggers container setup
- Expected: Complete interactive flow works

### E2E-2: Non-Interactive Container Setup
- File: `tests/test_simple_setup.py::TestE2E`
- What to test:
  - `run_setup()` with `non_interactive=True` and `deployment="container"`
  - No prompts displayed
  - Container setup proceeds automatically
- Expected: Silent non-interactive flow

### E2E-3: Container Setup with Docker
- File: `tests/test_simple_setup.py::TestE2E`
- What to test:
  - Container setup works with docker (not podman)
  - All docker commands replace podman equivalents
- Expected: Docker works as fallback

## Edge Cases

### EC-1: Existing Volume Name Conflict
- What to test:
  - Volume "agentalloy-data" already exists
  - `_create_volume()` handles duplicate volume error
- Expected: Either succeeds (volume exists) or fails gracefully

### EC-2: Existing Container Name Conflict
- What to test:
  - Container "agentalloy" already exists
  - `_run_container()` with `--replace` flag removes old container
- Expected: Old container replaced, new one starts

### EC-3: Port Already In Use
- What to test:
  - Port 47950 already in use
  - Preflight catches this
  - Error message suggests different port
- Expected: Clean failure before container run

### EC-4: Container Starts But Health Never Ready
- What to test:
  - Container runs but /health endpoint never returns 200
  - Health check times out after 300s
  - Error message includes container logs suggestion
- Expected: Clear error with troubleshooting steps

### EC-5: Container Crashes After Health Check
- What to test:
  - Container passes health check but crashes shortly after
  - State still recorded (container was healthy once)
  - User can diagnose via `agentalloy doctor`
- Expected: State recorded, error surfaced on next check

### EC-6: Auto-Clone Fails
- What to test:
  - No local build context, GitHub clone fails (network error, repo not found)
  - Error message explains how to provide build context manually
- Expected: Clear error with manual instructions

### EC-7: Partial State Recovery
- What to test:
  - Setup fails mid-flow (e.g., after build but before health check)
  - State file not corrupted
  - Next run can detect partial state and offer recovery
- Expected: State file integrity preserved

### EC-8: Multiple Deployment Modes in State
- What to test:
  - Old state has `deployment: "native"` with native fields
  - New state has `deployment: "container"` with container fields
  - `load_state()` handles both correctly
- Expected: No conflicts, correct keys used per deployment

### EC-9: Container Setup with Empty Packs
- What to test:
  - User selects no packs (empty string)
  - Container runs without AGENTIALLOY_PACKS env var
  - Setup completes successfully
- Expected: Empty packs handled correctly

### EC-10: Setup Interrupted During Build
- What to test:
  - User interrupts (SIGINT) during `podman build`
  - Build process terminated
  - No partial state written
  - Can retry without error
- Expected: Clean interrupt, retryable state
