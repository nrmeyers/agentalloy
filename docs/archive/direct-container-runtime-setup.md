# OBSOLETE — Pre-migration

This document describes the pre-GHCR-migration container setup (build-based approach).
It has been superseded by the GHCR pull-based flow. See:
- `design/container-ghcr-migration.md` — design doc
- `specs/container-ghcr-migration.md` — spec
- `plans/container-ghcr-migration-plan.md` — implementation plan

# Test Plan: Direct Container Runtime Setup

## 1. Unit Tests

### UT-1: Runtime Detection
- File: `tests/test_container_runtime.py`
- What to test: `_detect_runtime_binary()` returns "podman" when both podman and docker are on PATH; returns "docker" when only docker is available; returns None when neither is on PATH.
- Expected: Correct priority (podman > docker), None when no runtime available.

### UT-2: Build Context Location
- File: `tests/test_container_runtime.py`
- What to test: `_locate_build_context()` finds context in cwd first; falls back to parents[4] for editable installs; falls back to auto-clone when neither is found.
- Expected: Returns correct Path in each scenario; returns None when all sources fail.

### UT-3: Image Build Command
- File: `tests/test_container_runtime.py`
- What to test: `_build_image()` constructs the correct `podman build` command with `-t agentalloy:local -f Containerfile <context>`.
- Expected: Correct command construction, subprocess.run called with correct args, exit code propagated.

### UT-4: Volume Creation
- File: `tests/test_container_runtime.py`
- What to test: `_ensure_volume()` runs `{runtime} volume create agentalloy-data`; handles "volume already exists" error gracefully (idempotent).
- Expected: Volume created successfully; no error on "already exists".

### UT-5: Ollama Directory Creation
- File: `tests/test_container_runtime.py`
- What to test: `_ensure_ollama_dir()` creates `~/.ollama` if missing; no-op if already exists.
- Expected: Directory created with correct permissions; no error if exists.

### UT-6: Entrypoint Generation
- File: `tests/test_container_runtime.py`
- What to test: `_generate_entrypoint()` writes a valid bash script to a temp file with correct bootstrap logic (Ollama install, start, model pull, migrations, install-packs, uvicorn start).
- Expected: File exists, is executable, contains all bootstrap steps, temp file path returned.

### UT-7: Entrypoint Content — No Packs
- File: `tests/test_container_runtime.py`
- What to test: Generated entrypoint when `packs=""` — should not run install-packs step.
- Expected: No `install-packs` command in generated script when packs is empty.

### UT-8: Entrypoint Content — With Packs
- File: `tests/test_container_runtime.py`
- What to test: Generated entrypoint when `packs="foundation,tooling"` — should run `install-packs --packs foundation,tooling`.
- Expected: `install-packs --packs foundation,tooling` present in generated script.

### UT-9: Entrypoint Cleanup
- File: `tests/test_container_runtime.py`
- What to test: `_cleanup_temp_entrypoint()` removes the temp file.
- Expected: File removed after cleanup call.

### UT-10: Health Check Polling
- File: `tests/test_container_runtime.py`
- What to test: `_wait_for_health()` polls `/health` endpoint with exponential backoff; returns True on success; returns False on timeout.
- Expected: Returns True when /health returns 200; returns False after timeout; uses exponential backoff (2s, 4s, 8s, ...).

### UT-11: Runtime Detection Preflight — Podman Present
- File: `tests/test_preflight_container.py`
- What to test: `_check_runtime_binary()` passes when podman is on PATH.
- Expected: passed=True, detail includes podman path.

### UT-12: Runtime Detection Preflight — Docker Only
- File: `tests/test_preflight_container.py`
- What to test: `_check_runtime_binary()` passes when only docker is on PATH.
- Expected: passed=True, detail includes docker path.

### UT-13: Runtime Detection Preflight — Neither Present
- File: `tests/test_preflight_container.py`
- What to test: `_check_runtime_binary()` fails when neither podman nor docker is on PATH.
- Expected: passed=False, error message includes installation instructions.

### UT-14: Build Context Preflight — All Assets Present
- File: `tests/test_preflight_container.py`
- What to test: `_check_build_context()` passes when Containerfile, pyproject.toml, and uv.lock exist in build context.
- Expected: passed=True, detail lists found files.

### UT-15: Build Context Preflight — Missing Containerfile
- File: `tests/test_preflight_container.py`
- What to test: `_check_build_context()` fails when Containerfile is missing.
- Expected: passed=False, error mentions missing Containerfile.

### UT-16: Build Context Preflight — Missing pyproject.toml
- File: `tests/test_preflight_container.py`
- What to test: `_check_build_context()` fails when pyproject.toml is missing.
- Expected: passed=False, error mentions missing pyproject.toml.

### UT-17: Name Conflict Detection
- File: `tests/test_preflight_container.py`
- What to test: `_check_name_conflicts()` detects existing `agentalloy` container and fails preflight.
- Expected: passed=False, error includes container name and removal instructions.

### UT-18: Name Conflict Detection — No Conflict
- File: `tests/test_preflight_container.py`
- What to test: `_check_name_conflicts()` passes when no `agentalloy` container exists.
- Expected: passed=True, detail confirms no conflict.

### UT-19: Port Free Check
- File: `tests/test_preflight_container.py`
- What to test: `_check_port_free()` passes when port 47950 is free; fails when port is in use.
- Expected: passed=True when free; passed=False with remediation when in use.

### UT-20: State Schema Migration v3 -> v4
- File: `tests/test_state_migration.py`
- What to test: `_migrate()` removes compose fields and adds runtime fields when migrating from schema v3.
- Expected: compose_file/compose_binary/compose_binary_path removed; runtime_binary/image_tag/container_name/data_volume added.

### UT-21: SetupConfig — Removed Compose Fields
- File: `tests/test_simple_setup_container.py`
- What to test: `SetupConfig` no longer has `compose_binary` or `compose_file` attributes.
- Expected: AttributeError when accessing removed fields; new runtime fields accessible.

### UT-22: Fixed Values for Container Mode
- File: `tests/test_simple_setup_container.py`
- What to test: Container mode sets runner=ollama, port=47950, mode=manual, harness=manual.
- Expected: All fixed values applied correctly in container flow.

### UT-23: CPU-Only Warning
- File: `tests/test_simple_setup_container.py`
- What to test: Interactive container mode displays CPU-only warning and prompts for confirmation.
- Expected: Warning message displayed; setup aborted when user declines.

## 2. Integration Tests

### IT-1: Full Container Setup Flow — Happy Path
- File: `tests/test_container_integration.py`
- What to test: Complete container setup flow with mocked subprocess calls. All steps execute: runtime detection, build context location, preflight, image build, volume creation, entrypoint generation, container run, health check, state recording, verify, harness wiring.
- Expected: All mocked subprocess calls made in correct order; state recorded correctly; verify and wire_harness called.

### IT-2: Container Setup — Runtime Not Found
- File: `tests/test_container_integration.py`
- What to test: Setup fails gracefully when neither podman nor docker is on PATH.
- Expected: Exit code 1; error message displayed; no subprocess calls made after runtime detection failure.

### IT-3: Container Setup — Build Context Not Found
- File: `tests/test_container_integration.py`
- What to test: Setup fails when no build context found (no cwd clone, no editable install, auto-clone fails).
- Expected: Exit code 1; error message displayed; no image build attempted.

### IT-4: Container Setup — Image Build Failure
- File: `tests/test_container_integration.py`
- What to test: Setup fails when image build returns non-zero exit code.
- Expected: Exit code 1; last 30 lines of build output displayed; no container run attempted.

### IT-5: Container Setup — Container Start Failure
- File: `tests/test_container_integration.py`
- What to test: Setup fails when container run returns non-zero exit code.
- Expected: Exit code 1; error message displayed; state not recorded.

### IT-6: Container Setup — Health Check Timeout
- File: `tests/test_container_integration.py`
- What to test: Setup times out waiting for /health endpoint.
- Expected: Exit code 1; timeout message displayed; instructions to check container logs.

### IT-7: Container Setup — State Recording
- File: `tests/test_container_integration.py`
- What to test: State is recorded with correct values after successful setup: deployment=container, runtime_binary, image_tag, container_name, data_volume, port=47950.
- Expected: All state fields set correctly; compose fields removed.

### IT-8: Container Setup — Entrypoint Cleanup
- File: `tests/test_container_integration.py`
- What to test: Temp entrypoint file is cleaned up after container start (success or failure).
- Expected: Temp file removed; no orphaned temp files.

### IT-9: Container Setup — Entrypoint Content Verification
- File: `tests/test_container_integration.py`
- What to test: Generated entrypoint script contains correct bootstrap logic.
- Expected: Script contains ollama install, start, model pull, migrations, install-packs, uvicorn start, SIGTERM trap.

### IT-10: Preflight Container Phase — All Checks Pass
- File: `tests/test_preflight_container.py`
- What to test: Container preflight passes when all checks pass (runtime, build context, port free, no name conflicts).
- Expected: all_checks_passed=True; no fatal failures.

### IT-11: Preflight Container Phase — Mixed Failures
- File: `tests/test_preflight_container.py`
- What to test: Container preflight fails when some checks fail (runtime OK, build context missing).
- Expected: all_checks_passed=False; specific failures listed with remediation.

### IT-12: Day-2 Operation — Reembed in Container
- File: `tests/test_container_integration.py`
- What to test: Reembed in container mode: stop service, run reembed, restart service.
- Expected: stop_service_in_container() called, reembed runs, restart_service_in_container() called.

### IT-13: Day-2 Operation — Install-Packs in Container
- File: `tests/test_container_integration.py`
- What to test: Install-packs in container mode: stop service, install packs, restart service.
- Expected: stop_service_in_container() called, install-packs runs, restart_service_in_container() called.

### IT-14: Day-2 Operation — --no-restart Flag
- File: `tests/test_container_integration.py`
- What to test: --no-restart flag suppresses restart in container mode.
- Expected: stop_service_in_container() called, restart_service_in_container() NOT called.

## 3. End-to-End Tests

### E2E-1: Container Setup — Full Integration (Mocked Runtime)
- File: `tests/test_container_e2e.py`
- What to test: Full container setup with mocked runtime binary (podman/docker) that simulates successful build, volume creation, and container start.
- Expected: All steps complete; /health endpoint becomes reachable; state recorded; verify passes.

### E2E-2: Container Setup — Model Pull in Container
- File: `tests/test_container_e2e.py`
- What to test: Container bootstrap pulls qwen3-embedding:0.6b model on first boot.
- Expected: Model pull command executed; model cached in ~/.ollama; bootstrap-complete flag created.

### E2E-3: Container Bootstrap Idempotency
- File: `tests/test_container_e2e.py`
- What to test: Container restart skips Ollama install, model pull, migrations, and install-packs when /app/.bootstrap-complete exists.
- Expected: Only uvicorn starts; no redundant operations.

### E2E-4: Container Bootstrap — Crash Recovery
- File: `tests/test_container_e2e.py`
- What to test: Container restart after crash mid-bootstrap (no .bootstrap-complete flag) re-runs migrations and install-packs.
- Expected: Migrations run (idempotent); install-packs runs (idempotent); .bootstrap-complete created.

## 4. Edge Cases

### EC-1: Existing Container with Same Name
- File: `tests/test_container_edge_cases.py`
- What to test: Setup detects existing `agentalloy` container and handles it (remove and continue, or abort).
- Expected: Existing container removed; setup continues.

### EC-2: Existing Volume
- File: `tests/test_container_edge_cases.py`
- What to test: `podman volume create agentalloy-data` when volume already exists.
- Expected: No error; setup continues.

### EC-3: Port Already in Use
- File: `tests/test_container_edge_cases.py`
- What to test: Setup fails when port 47950 is already in use.
- Expected: Preflight fails with port conflict error; remediation instructions displayed.

### EC-4: Auto-Clone Fails
- File: `tests/test_container_edge_cases.py`
- What to test: Auto-clone to ~/.cache/agentalloy/repo fails (no git, network error, etc.).
- Expected: Clear error message; no crash.

### EC-5: Entrypoint Script Write Failure
- File: `tests/test_container_edge_cases.py`
- What to test: Temp file write fails (disk full, permission denied).
- Expected: Clear error message; no orphaned temp file.

### EC-6: Health Check Intermittent Failures
- File: `tests/test_container_edge_cases.py`
- What to test: /health endpoint returns non-200 for first few polls, then succeeds.
- Expected: Health check retries until success; timeout after 300s.

### EC-7: Entrypoint Script — Ollama Already Installed
- File: `tests/test_container_edge_cases.py`
- What to test: Container bootstrap when Ollama is already installed (skip install step).
- Expected: Ollama start proceeds directly; no redundant install.

### EC-8: Entrypoint Script — Model Already Cached
- File: `tests/test_container_edge_cases.py`
- What to test: Container bootstrap when qwen3-embedding:0.6b is already cached (skip pull step).
- Expected: Model pull skipped; bootstrap continues to migrations.

### EC-9: Entrypoint Script — Migrations Already Run
- File: `tests/test_container_edge_cases.py`
- What to test: Container bootstrap when /app/.bootstrap-complete exists (skip all steps except uvicorn start).
- Expected: Direct jump to uvicorn start; no migrations, no install-packs.

### EC-10: Entrypoint Script — SIGTERM Handling
- File: `tests/test_container_edge_cases.py`
- What to test: Container receives SIGTERM; uvicorn shuts down gracefully.
- Expected: SIGTERM trap fires; uvicorn stops; container exits cleanly.

### EC-11: Apple Silicon Ollama Installation
- File: `tests/test_container_edge_cases.py`
- What to test: Entrypoint on Apple Silicon detects platform and uses brew install --cask ollama-app.
- Expected: brew install --cask ollama-app executed; ollama installed and started.

### EC-12: Rootless Podman Compatibility
- File: `tests/test_container_edge_cases.py`
- What to test: All podman commands work with rootless podman (no sudo).
- Expected: No root privileges required; commands succeed with rootless podman.

### EC-13: Docker vs Podman Command Differences
- File: `tests/test_container_edge_cases.py`
- What to test: All runtime commands work with both podman and docker (same CLI syntax).
- Expected: Commands use `{runtime} build`, `{runtime} volume create`, etc.; no runtime-specific branches.

### EC-14: Non-Interactive Mode
- File: `tests/test_container_edge_cases.py`
- What to test: Setup with --non-interactive flag accepts all defaults.
- Expected: No prompts; all defaults accepted; setup completes.

### EC-15: Cancel During CPU-Only Warning
- File: `tests/test_container_edge_cases.py`
- What to test: User declines CPU-only warning in interactive mode.
- Expected: Setup aborted; no further action taken.

### EC-16: Cancel During Review
- File: `tests/test_container_edge_cases.py`
- What to test: User declines review confirmation in interactive mode.
- Expected: Setup aborted; no image build or container run.
