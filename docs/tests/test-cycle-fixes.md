# Test Plan: Test Cycle Fixes for Container Entrypoint

## Scope

Tests for two container deployment fixes in `_build_entrypoint_script()`:

- **Issue 3**: Empty packs now triggers `agentalloy install-packs --non-interactive --no-restart` instead of skipping.
- **Issue 4**: Uvicorn starts AFTER pack installation + bootstrap complete, eliminating the LadybugDB lock conflict.

## Test Categories

### 1. Unit Tests — Entrypoint Script Generation

These tests call `_build_entrypoint_script(packs)` and assert on the returned bash string. No subprocess execution.

#### UT-1: Empty Packs — Install Always-On Packs

**Test**: `test_empty_packs_installs_always_on`
- **Input**: `packs=""`
- **Assert**: Script contains `agentalloy install-packs --non-interactive --no-restart`
- **Assert**: Script does NOT contain `"No packs specified - skipping pack installation"`
- **Assert**: Script contains `echo ">> Installing always-on packs..."`

#### UT-2: Empty Packs — No Per-Pack Loop

**Test**: `test_empty_packs_no_per_pack_loop`
- **Input**: `packs=""`
- **Assert**: Script does NOT contain `PACK_LIST=`
- **Assert**: Script does NOT contain `for pack in "${PACK_LIST[@]}"`
- **Assert**: Script does NOT contain `Installing pack:`

#### UT-3: Non-Empty Packs — Per-Pack Loop Still Works

**Test**: `test_nonempty_packs_uses_per_pack_loop`
- **Input**: `packs="core,documentation"`
- **Assert**: Script contains `PACK_LIST=(core documentation)`
- **Assert**: Script contains `TOTAL=2`
- **Assert**: Script contains `for pack in "${PACK_LIST[@]}"`
- **Assert**: Script contains `uv run agentalloy install-packs --packs "$pack" --no-restart`
- **Assert**: Script does NOT contain `agentalloy install-packs --non-interactive --no-restart`

#### UT-4: Uvicorn After Bootstrap Complete

**Test**: `test_uvicorn_after_bootstrap_complete`
- **Input**: `packs="core"`
- **Assert**: Line index of `touch "$COMPLETE"` is LESS than line index of `uv run uvicorn`
- **Assert**: Line index of `echo ">> Bootstrap complete"` is LESS than line index of `uv run uvicorn`
- **Assert**: The `echo ">> Bootstrap complete"` line is inside the `if [ "$BOOTSTRAP_NEEDED" = "true" ]` block
- **Assert**: The `uv run uvicorn` line is OUTSIDE the `if [ "$BOOTSTRAP_NEEDED" = "true" ]` block (after `fi`)

#### UT-5: Uvicorn Not Before Pack Install

**Test**: `test_uvicorn_not_before_pack_install`
- **Input**: `packs="core,documentation"`
- **Assert**: Line index of `uv run uvicorn` is GREATER than line index of `install-packs --packs "$pack" --no-restart`
- **Assert**: Line index of `uv run uvicorn` is GREATER than line index of `pack_ingested`

#### UT-6: Uvicorn After Migrations

**Test**: `test_uvicorn_after_migrations`
- **Input**: `packs=""`
- **Assert**: Line index of `uv run uvicorn` is GREATER than line index of `agentalloy.migrate`

#### UT-7: SIGTERM Trap Position

**Test**: `test_sigterm_trap_before_uvicorn`
- **Input**: `packs=""`
- **Assert**: Line index of `trap 'kill` is LESS than line index of `UVICORN_PID=$!`
- **Assert**: Trap still covers both `OLLAMA_PID` and `UVICORN_PID`

#### UT-8: Script Passes Bash Syntax Check

**Test**: `test_script_passes_bash_syntax_check`
- **Input**: `packs=""`, `packs="core"`, `packs="python,nodejs,rust"`
- **Assert**: `bash -n` returns exit code 0 for each
- **Skip**: If `bash` not on PATH

#### UT-9: Empty Packs Branch Has Echo + Command

**Test**: `test_empty_packs_branch_has_both_echo_and_command`
- **Input**: `packs=""`
- **Assert**: Script contains `echo ">> Installing always-on packs..."`
- **Assert**: Script contains `uv run agentalloy install-packs --non-interactive --no-restart`
- **Assert**: The echo line appears BEFORE the install-packs command

#### UT-10: Uvicorn Start Comment Updated

**Test**: `test_uvicorn_start_comment_updated`
- **Input**: `packs=""`
- **Assert**: Script contains `# --- Start uvicorn AFTER all bootstrap steps`
- **Assert**: Script does NOT contain `# Start uvicorn BEFORE pack ingest`
- **Assert**: Script does NOT contain `# --- Fast-start uvicorn`

#### UT-11: No Packs — Complete Marker Still Written

**Test**: `test_no_packs_complete_marker_written`
- **Input**: `packs=""`
- **Assert**: Script contains `touch "$COMPLETE"`
- **Assert**: `touch "$COMPLETE"` appears inside the `if [ "$BOOTSTRAP_NEEDED" = "true" ]` block

#### UT-12: No Packs — Lock Still Cleared

**Test**: `test_no_packs_lock_cleared`
- **Input**: `packs=""`
- **Assert**: Script contains `rm -f "$LOCK"`
- **Assert**: `rm -f "$LOCK"` appears inside the `if [ "$BOOTSTRAP_NEEDED" = "true" ]` block, before `touch "$COMPLETE"`

#### UT-13: Existing Tests Still Pass — Lock at Start

**Test**: `test_lock_at_start`
- **Input**: `packs="python,nodejs"`
- **Assert**: Script contains `date -Iseconds > "$LOCK"`
- **Assert**: Script contains `LOCK="$APP_DIR/.bootstrap-lock"`

#### UT-14: Existing Tests Still Pass — Atomic Progress Writes

**Test**: `test_atomic_progress_writes`
- **Input**: `packs="python"`
- **Assert**: Script contains `PROGRESS_TMP="$APP_DIR/.bootstrap-progress.tmp"`
- **Assert**: Script contains `mv "$PROGRESS_TMP" "$PROGRESS"`

#### UT-15: Existing Tests Still Pass — Checkpoints

**Test**: `test_checkpoints_after_each_pack`
- **Input**: `packs="python,nodejs"`
- **Assert**: Script contains `pack_ingested`
- **Assert**: Script contains `>> "$CHECKPOINTS"`

#### UT-16: Existing Tests Still Pass — Stale Lock Detection

**Test**: `test_stale_lock_detection`
- **Input**: `packs="python"`
- **Assert**: Script contains `7200`
- **Assert**: Script contains `Stale bootstrap lock detected`
- **Assert**: Script contains `rm -f "$LOCK" "$CHECKPOINTS"`

#### UT-17: Existing Tests Still Pass — Checkpoint Resume

**Test**: `test_checkpoint_resume`
- **Input**: `packs="python,nodejs"`
- **Assert**: Script contains `pack_already_done`
- **Assert**: Script contains `grep -Fq`
- **Assert**: Script contains `already ingested - skipping`

#### UT-18: Existing Tests Still Pass — Corrupt Checkpoints

**Test**: `test_corrupt_checkpoints_treated_as_none`
- **Input**: `packs="python"`
- **Assert**: Script contains `|| echo 0`

#### UT-19: SIGTERM Trap Kills Both PIDs

**Test**: `test_sigterm_traps_both_pids`
- **Input**: `packs=""`
- **Assert**: Script contains `kill ${OLLAMA_PID:-} ${UVICORN_PID:-}`

#### UT-20: Entrypoint File Permissions

**Test**: `test_entrypoint_permissions`
- **Input**: Call `_generate_entrypoint("")`
- **Assert**: File permissions are `0o700`

### 2. Integration Tests — Generated Script Logic

These tests execute the generated bash script in a controlled environment to verify the flow.

#### IT-1: Script Executes Without Syntax Errors

**Test**: `test_script_executes_cleanly`
- **Setup**: Generate script with `packs=""`, write to temp file
- **Execute**: `bash /tmp/test-entrypoint.sh`
- **Assert**: Script exits with code 0 (it will fail at the `ollama`/`uvicorn` steps, but we test the structure)
- **Note**: Full execution requires mocking Ollama and uvicorn. See IT-2.

#### IT-2: Bootstrap Completes Without Uvicorn During Pack Install

**Test**: `test_no_uvicorn_during_bootstrap`
- **Setup**: Create a mock environment with:
  - `APP_DIR=/tmp/test-app`
  - Mock `ollama` binary (exits 0 immediately)
  - Mock `curl` (returns success for Ollama health check)
  - Mock `uv` (exits 0 for migrations)
  - Mock `agentalloy` (exits 0 for install-packs)
  - Mock `uvicorn` (does NOT run — verify it's never called during bootstrap)
- **Execute**: Run the generated script
- **Assert**: `.bootstrap-complete` file is created
- **Assert**: `.bootstrap-lock` file is removed
- **Assert**: `agentalloy install-packs --non-interactive --no-restart` is called (if empty packs)
- **Assert**: No LadybugDB lock conflict errors

#### IT-3: Per-Pack Install Works Correctly

**Test**: `test_per_pack_install_in_script`
- **Setup**: Generate script with `packs="core,documentation"`, create mock environment
- **Execute**: Run the generated script
- **Assert**: `agentalloy install-packs --packs core --no-restart` is called
- **Assert**: `agentalloy install-packs --packs documentation --no-restart` is called
- **Assert**: Checkpoints are written for both packs
- **Assert**: `.bootstrap-complete` is created after both packs

#### IT-4: Checkpoint Resume Skips Already Installed Packs

**Test**: `test_checkpoint_resume_skips_installed`
- **Setup**: Generate script with `packs="core,documentation"`, create:
  - `.bootstrap-checkpoints` with `core` already checkpointed
  - `.bootstrap-lock` with recent timestamp
- **Execute**: Run the generated script
- **Assert**: `agentalloy install-packs --packs core` is NOT called
- **Assert**: `agentalloy install-packs --packs documentation` IS called
- **Assert**: Script completes successfully

#### IT-5: Stale Lock Recovery Works

**Test**: `test_stale_lock_recovery`
- **Setup**: Generate script with `packs=""`, create:
  - `.bootstrap-lock` with mtime > 2 hours ago
  - `.bootstrap-checkpoints` with stale data
- **Execute**: Run the generated script
- **Assert**: Script detects stale lock and starts fresh
- **Assert**: `.bootstrap-lock` and `.bootstrap-checkpoints` are removed
- **Assert**: Bootstrap proceeds normally

#### IT-6: Bootstrap Already Complete — Skip All Steps

**Test**: `test_bootstrap_already_complete`
- **Setup**: Generate script with `packs=""`, create:
  - `.bootstrap-complete` file
- **Execute**: Run the generated script
- **Assert**: Script skips Ollama install, pack install, migrations
- **Assert**: Script starts uvicorn directly
- **Assert**: Script exits normally

### 3. Edge Case Tests

#### EC-1: Stale Lock from Crashed Container

**Test**: `test_stale_lock_from_crashed_container`
- **Setup**: Generate script with `packs="core,documentation,engineering"`, create:
  - `.bootstrap-lock` with mtime 3 hours ago
  - `.bootstrap-checkpoints` with only `core` checkpointed
- **Execute**: Run the generated script
- **Assert**: Stale lock is detected and wiped
- **Assert**: All packs are re-installed from scratch (not resumed)
- **Assert**: Bootstrap completes successfully

#### EC-2: Partial Bootstrap — Crash Mid-Pack

**Test**: `test_partial_bootstrap_crash_mid_pack`
- **Setup**: Generate script with `packs="core,documentation,engineering"`, create:
  - `.bootstrap-lock` with recent timestamp
  - `.bootstrap-checkpoints` with `core` checkpointed
- **Execute**: Run the generated script
- **Assert**: `core` is skipped (already checkpointed)
- **Assert**: `documentation` and `engineering` are installed
- **Assert**: `.bootstrap-complete` is created

#### EC-3: Empty Container Data Volume

**Test**: `test_empty_container_data_volume`
- **Setup**: Generate script with `packs=""`, use a fresh empty `APP_DIR`
- **Execute**: Run the generated script (with mocked Ollama, uv, migrations)
- **Assert**: Migrations run successfully
- **Assert**: `agentalloy install-packs --non-interactive --no-restart` runs
- **Assert**: `.bootstrap-complete` is created
- **Assert**: No errors from missing database files

#### EC-4: Existing Data Volume from Prior Version

**Test**: `test_existing_data_volume_from_prior_version`
- **Setup**: Generate script with `packs=""`, create:
  - `.bootstrap-complete` file (simulating prior version)
  - Existing database files in `APP_DIR/data/`
- **Execute**: Run the generated script
- **Assert**: Bootstrap is skipped entirely
- **Assert**: `.bootstrap-complete` is NOT modified
- **Assert**: Database files are NOT touched
- **Assert**: Uvicorn starts directly

#### EC-5: Corrupt Checkpoint File

**Test**: `test_corrupt_checkpoint_file`
- **Setup**: Generate script with `packs="core,documentation"`, create:
  - `.bootstrap-checkpoints` with invalid content (`not json at all!!!`)
  - `.bootstrap-lock` with recent timestamp
- **Execute**: Run the generated script
- **Assert**: Script does NOT crash on corrupt checkpoint
- **Assert**: Both packs are installed (corrupt file treated as "no checkpoints")
- **Assert**: `.bootstrap-complete` is created

#### EC-6: SIGTERM During Pack Install

**Test**: `test_sigterm_during_pack_install`
- **Setup**: Generate script with `packs="core,documentation"`, create:
  - `.bootstrap-lock` with recent timestamp
  - Mock `agentalloy` that sleeps for 60s (simulates long pack install)
- **Execute**: Run the generated script in background
- **Send**: SIGTERM after 1 second
- **Assert**: Script exits cleanly (trap fires)
- **Assert**: Lock file is NOT removed (bootstrap incomplete)
- **Assert**: Next run will retry from checkpoint

#### EC-7: Multiple Container Restarts

**Test**: `test_multiple_container_restarts`
- **Setup**: Generate script with `packs="core"`, create:
  - `.bootstrap-lock` with mtime 3 hours ago (stale)
  - `.bootstrap-checkpoints` with `core` checkpointed
- **Execute**: Run the generated script (first restart)
- **Assert**: Stale lock wiped, `core` re-installed, `.bootstrap-complete` created
- **Execute**: Run the generated script again (second restart)
- **Assert**: Bootstrap skipped (`.bootstrap-complete` exists)
- **Assert**: Uvicorn starts directly

#### EC-8: Packs Flag with Spaces or Extra Commas

**Test**: `test_packs_flag_with_spaces_and_extra_commas`
- **Input**: `packs="core, ,documentation,  ,engineering"`
- **Assert**: Script contains `PACK_LIST=(core documentation engineering)`
- **Assert**: Script contains `TOTAL=3`
- **Assert**: No empty pack names in the loop

#### EC-9: Packs Flag with Special Characters

**Test**: `test_packs_flag_with_special_characters`
- **Input**: `packs="core,python"`
- **Assert**: Pack names are properly shell-quoted in the array literal
- **Assert**: Script passes `bash -n` syntax check

### 4. Host-Side Readiness Polling Tests

These tests verify that `_wait_for_readiness()` handles the new sequence correctly.

#### HR-1: Readiness Polling Handles Uvicorn Not Yet Started

**Test**: `test_readiness_polling_handles_uvicorn_not_started`
- **Setup**: Mock `urllib.request.urlopen` to raise `URLError` for first 3 calls, then return `{"status": "ready"}`
- **Execute**: `_wait_for_readiness(47950, timeout=60, poll_interval=0.1)`
- **Assert**: Returns `True` after connection errors resolve
- **Assert**: Polling continues through connection errors

#### HR-2: Readiness Polling Times Out Correctly

**Test**: `test_readiness_polling_timeout`
- **Setup**: Mock `urllib.request.urlopen` to always raise `URLError`
- **Execute**: `_wait_for_readiness(47950, timeout=5, poll_interval=0.1)`
- **Assert**: Returns `False` after timeout

#### HR-3: Readiness Polling Handles Warming Up Then Ready

**Test**: `test_readiness_polling_warming_up_then_ready`
- **Setup**: Mock responses: `warming_up` (2x), then `ready`
- **Execute**: `_wait_for_readiness(47950, timeout=60, poll_interval=0.1)`
- **Assert**: Returns `True`
- **Assert**: Progress callback receives both `warming_up` and `ready` events

### 5. Backward Compatibility Tests

#### BC-1: Existing Container — Bootstrap Already Complete

**Test**: `test_existing_container_bootstrap_complete`
- **Setup**: Mount an existing data volume with `.bootstrap-complete`
- **Execute**: Start container with new entrypoint
- **Assert**: Container starts uvicorn immediately (no bootstrap)
- **Assert**: No pack installation occurs
- **Assert**: No database corruption

#### BC-2: Existing Container — No Bootstrap Complete Flag

**Test**: `test_existing_container_no_bootstrap_flag`
- **Setup**: Mount an existing data volume WITHOUT `.bootstrap-complete`
- **Execute**: Start container with new entrypoint
- **Assert**: Bootstrap runs (Ollama, migrations, packs)
- **Assert**: Pack installation completes
- **Assert**: `.bootstrap-complete` is created
- **Assert**: Uvicorn starts after bootstrap

#### BC-3: Container with No Packs — Always-On Packs Installed

**Test**: `test_container_no_packs_installs_always_on`
- **Setup**: Fresh container data volume, `AGENTIALLOY_PACKS=""`
- **Execute**: Start container with new entrypoint
- **Assert**: `agentalloy install-packs --non-interactive --no-restart` runs
- **Assert**: Always-on packs (core, documentation, engineering, performance, refactoring) are installed
- **Assert**: Container reports >= 5 skills after bootstrap

#### BC-4: Container with Explicit Packs — Per-Pack Install

**Test**: `test_container_with_packs_installs_all`
- **Setup**: Fresh container data volume, `AGENTIALLOY_PACKS="core,documentation"`
- **Execute**: Start container with new entrypoint
- **Assert**: Per-pack loop runs for `core` and `documentation`
- **Assert**: No lock conflict errors
- **Assert**: All specified packs are installed
- **Assert**: `.bootstrap-complete` is created

### 6. Regression Tests (Update Existing Tests)

The following existing tests in `tests/install/test_container_runtime_readiness.py` need expectation updates:

#### RT-1: Update `test_ut10_uvicorn_starts_before_pack_ingest`

**Current expectation**: `uvicorn_idx < ingest_idx` (uvicorn starts BEFORE pack ingest)
**New expectation**: `uvicorn_idx > ingest_idx` (uvicorn starts AFTER pack ingest)

```python
def test_ut10_uvicorn_starts_after_pack_ingest(self) -> None:
    script = _build_entrypoint_script("python,nodejs")
    uvicorn_idx = script.find("uvicorn agentalloy.app:app")
    ingest_idx = script.find("Installing pack")
    assert uvicorn_idx != -1 and ingest_idx != -1, script
    assert uvicorn_idx > ingest_idx, "uvicorn must start AFTER pack ingest (lock fix)"
```

#### RT-2: Update `test_ec12_ec13_no_packs_path`

**Current expectation**: Script contains `"No packs specified"`
**New expectation**: Script contains `"Installing always-on packs"` and `install-packs --non-interactive`

```python
def test_ec12_ec13_no_packs_path(self) -> None:
    script = _build_entrypoint_script("")
    assert "Installing always-on packs" in script
    assert "install-packs --non-interactive" in script
    # Still wires uvicorn + complete marker even with no packs.
    assert "uvicorn agentalloy.app:app" in script
    assert 'touch "$COMPLETE"' in script
```

#### RT-3: Update `tests/test_container_edge_cases.py` — EC-9

**Current expectation**: `assert ollama_install < uvicorn_start`
**New expectation**: `assert uvicorn_start > complete_marker` (uvicorn starts AFTER bootstrap complete)

```python
def test_entrypoint_skips_bootstrap_when_flag_exists(self):
    from agentalloy.install.subcommands.container_runtime import _build_entrypoint_script

    script = _build_entrypoint_script("")
    bootstrap_check = script.index(".bootstrap-complete")
    ollama_install = script.index("ollama.ai/install.sh")
    uvicorn_start = script.index("uv run uvicorn agentalloy.app:app")
    complete_marker = script.index('touch "$COMPLETE"')
    # Bootstrap check before ollama install (unchanged)
    assert bootstrap_check < ollama_install
    # Uvicorn starts AFTER bootstrap complete (new order)
    assert complete_marker < uvicorn_start
```

## Test Execution Order

1. **Unit tests first** (fast, no external dependencies)
2. **Integration tests** (require mocked environment)
3. **Edge case tests** (require specific file states)
4. **Host-side readiness tests** (require HTTP mock)
5. **Backward compatibility tests** (require container runtime or full mock)
6. **Regression tests** (update existing test expectations)

## Test Data Requirements

- **Mock binaries**: `ollama`, `uv`, `uvicorn`, `agentalloy` — all exit 0
- **Mock HTTP server**: For `/readiness` endpoint testing
- **Temp directories**: For `.bootstrap-*` file creation and manipulation
- **No real container runtime required** for unit and integration tests

## Automated Test Invocation

```bash
# Unit tests only
pytest tests/install/test_container_runtime_readiness.py -v -k "test_ut"

# All container runtime tests
pytest tests/install/test_container_runtime_readiness.py -v
pytest tests/test_container_edge_cases.py -v

# Full test suite (includes regression updates)
pytest tests/ -v -k "container"
```

## Acceptance Criteria

1. All unit tests pass — `_build_entrypoint_script()` generates correct bash for all pack configurations.
2. All integration tests pass — generated script logic is correct (bootstrap sequence, lock handling, checkpoint resume).
3. All edge case tests pass — stale locks, corrupt checkpoints, partial bootstrap, empty/existing volumes.
4. All backward compatibility tests pass — existing containers work correctly.
5. All regression tests updated — existing test expectations match the new script order.
6. No new test failures introduced — the test suite is a net positive.
