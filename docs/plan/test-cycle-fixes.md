# Plan: Test Cycle Fixes

**Branch**: `feature/test-cycle-fixes`
**Source files**:
- Spec: `docs/specs/test-cycle-fixes.md`
- Design: `docs/design/test-cycle-fixes.md`
- Test Plan: `docs/tests/test-cycle-fixes.md`

## Phase Summary

### Phase 1: Empty Packs — Install Always-On Packs (Fixes Issue 3 / C03)
- **Description**: Replace the empty-packs skip (`echo "No packs specified - skipping pack installation"`) with a call to `agentalloy install-packs --non-interactive --no-restart`, which installs the always-on packs (core, documentation, engineering, performance, refactoring) via `_select_packs()` line 402-404.
- **Task IDs**: T1, T2
- **Owned criteria**: C03 (Container Default Packs Installed), C04 (No LadybugDB Lock Conflict — partial)
- **Dependencies**: None

### Phase 2: Reorder Entrypoint — Uvicorn After Bootstrap (Fixes Issue 4 / C04)
- **Description**: Move the uvicorn start block from before the pack installation block to after the bootstrap-complete block. This eliminates the LadybugDB lock conflict because uvicorn is not running when `install-packs` writes to the database.
- **Task IDs**: T3, T4
- **Owned criteria**: C04 (No LadybugDB Lock Conflict), C05 (Backward Compatibility)
- **Dependencies**: Phase 1 (T1, T2) — the uvicorn reordering must be done together with the empty-packs fix to produce a valid script

### Phase 3: Update Docstring + Regression Tests
- **Description**: Update the function docstring to reflect the new bootstrap sequence, and update existing test expectations in the regression test plan.
- **Task IDs**: T5, T6
- **Owned criteria**: C05 (Backward Compatibility), "No regression in existing tests"
- **Dependencies**: Phase 2 — docstring describes the final state of the code

### Phase 4: Integration, Edge Case, and Backward Compatibility Tests
- **Description**: Write integration tests that execute the generated script in a mock environment, edge case tests for stale locks, corrupt checkpoints, and partial bootstrap, host-side readiness polling tests, and backward compatibility tests.
- **Task IDs**: T7, T8
- **Owned criteria**: C03, C04, C05
- **Dependencies**: Phase 2 — tests validate the complete new behavior

---

## Build Tasks

### T1: Empty Packs — Replace Skip with install-packs Call

**Design doc section**: Change 2 (lines 178-191)
**Spec reference**: REQ-3 (lines 119-156)

**Description**: In `_build_entrypoint_script()` at `src/agentalloy/install/subcommands/container_runtime.py` lines 474-475, replace the empty-packs skip branch with a call to `agentalloy install-packs --non-interactive --no-restart`. The `--non-interactive` flag causes `_select_packs()` (line 402-404 of `install_packs.py`) to install only always-on packs.

**Files to modify**:
- `src/agentalloy/install/subcommands/container_runtime.py` (lines 474-475)

**Tests to write (TDD)**:
- UT-1: `test_empty_packs_installs_always_on` — script contains `agentalloy install-packs --non-interactive --no-restart`, does NOT contain `No packs specified - skipping`
- UT-2: `test_empty_packs_no_per_pack_loop` — script does NOT contain `PACK_LIST=`, `for pack in`, `Installing pack:`
- UT-9: `test_empty_packs_branch_has_both_echo_and_command` — echo line appears before install-packs command
- RT-2: Update `test_ec12_ec13_no_packs_path` — assert `Installing always-on packs` and `install-packs --non-interactive` in script

**TDD instructions**:
1. **RED**: Write `test_empty_packs_installs_always_on` that asserts `agentalloy install-packs --non-interactive --no-restart` is in the script when `packs=""`. Run — it should fail because the current code has the skip echo.
2. **GREEN**: Replace lines 474-475:
   ```python
   # Before (line 474-475):
   else:
       lines.append('    echo ">> No packs specified - skipping pack installation"')

   # After:
   else:
       lines.append('        echo ">> Installing always-on packs..."')
       lines.append('        uv run agentalloy install-packs --non-interactive --no-restart')
   ```
3. **REFACTOR**: Run all UT tests. Verify `test_it2_script_passes_bash_syntax_check` still passes. Verify `test_ut10_uvicorn_starts_before_pack_ingest` still passes (uvicorn ordering is unchanged).

---

### T2: Non-Empty Packs — Per-Pack Loop Unchanged

**Design doc section**: Change 1 (lines 138-176), NO CHANGE to per-pack loop body
**Spec reference**: REQ-4 (lines 158-218)

**Description**: Verify that the per-pack loop (lines 446-471) is unaffected by the changes. The pack array literal, per-pack install command, checkpoint writing, and progress tracking remain identical. This task is primarily verification + regression guards.

**Files to verify (no changes expected)**:
- `src/agentalloy/install/subcommands/container_runtime.py` (lines 446-471)

**Tests to write (TDD)**:
- UT-3: `test_nonempty_packs_uses_per_pack_loop` — script contains `PACK_LIST=(core documentation)`, `TOTAL=2`, per-pack loop, does NOT contain `--non-interactive`
- UT-13: `test_lock_at_start` — `date -Iseconds > "$LOCK"` present
- UT-14: `test_atomic_progress_writes` — `PROGRESS_TMP` and `mv` present
- UT-15: `test_checkpoints_after_each_pack` — `pack_ingested` and `>> "$CHECKPOINTS"` present
- UT-16: `test_stale_lock_detection` — `7200` and `Stale bootstrap lock detected` present
- UT-17: `test_checkpoint_resume` — `pack_already_done`, `grep -Fq`, `already ingested - skipping` present
- UT-18: `test_corrupt_checkpoints_treated_as_none` — `|| echo 0` present

**TDD instructions**:
1. **RED**: Write `test_nonempty_packs_uses_per_pack_loop` that asserts the per-pack loop structure with `packs="core,documentation"`. This test should already pass — it's a guard test to ensure we don't accidentally break the per-pack loop.
2. **GREEN**: If the test passes, no code changes needed. If any regression appears, fix it.
3. **REFACTOR**: Add all UT-13 through UT-18 as regression guard tests in `tests/install/test_container_runtime_readiness.py`.

---

### T3: Uvicorn — Move After Bootstrap Complete

**Design doc section**: Change 1 (lines 138-176) and Change 3 (lines 193-235)
**Spec reference**: REQ-4 (lines 158-218)

**Description**: Move the uvicorn start block from lines 435-441 (before the bootstrap pack-install block) to after line 484 (after the `fi` that closes the `if [ "$BOOTSTRAP_NEEDED" = "true" ]` block). Also update the comment from "Fast-start uvicorn" to "Start uvicorn AFTER all bootstrap steps".

**Files to modify**:
- `src/agentalloy/install/subcommands/container_runtime.py`
  - Lines 435-441: Remove uvicorn start block
  - Lines 477-488: Insert uvicorn start block after bootstrap-complete `fi`

**Tests to write (TDD)**:
- UT-4: `test_uvicorn_after_bootstrap_complete` — `touch "$COMPLETE"` line index < `uv run uvicorn` line index; uvicorn is outside the `if [ "$BOOTSTRAP_NEEDED" ]` block
- UT-5: `test_uvicorn_not_before_pack_install` — uvicorn line index > `install-packs --packs` line index
- UT-6: `test_uvicorn_after_migrations` — uvicorn line index > `agentalloy.migrate` line index
- UT-10: `test_uvicorn_start_comment_updated` — contains `# --- Start uvicorn AFTER all bootstrap steps`, does NOT contain `# Start uvicorn BEFORE pack ingest` or `# --- Fast-start uvicorn`
- RT-1: Update `test_ut10_uvicorn_starts_before_pack_ingest` → `test_ut10_uvicorn_starts_after_pack_ingest` — assert `uvicorn_idx > ingest_idx`

**TDD instructions**:
1. **RED**: Write `test_uvicorn_after_bootstrap_complete` that asserts `touch "$COMPLETE"` appears before `uv run uvicorn` in the script. Run — it should fail because the current code has uvicorn at line 440 and `touch "$COMPLETE"` at line 482.
2. **GREEN**: Make three edits:
   a. Remove lines 435-441 (the uvicorn start block). The lines 432-434 (SIGTERM trap) and line 443 (`if [ "$BOOTSTRAP_NEEDED" = "true" ]; then`) remain.
   b. In lines 477-488, after the `fi` (line 484), insert:
      ```python
      "",
      "# --- Start uvicorn AFTER all bootstrap steps ---------------------",
      "# Uvicorn starts after pack installation + bootstrap complete to",
      "# avoid LadybugDB lock conflict. The /readiness endpoint will only",
      "# become reachable after bootstrap finishes.",
      'echo ">> Starting uvicorn..."',
      "uv run uvicorn agentalloy.app:app --host 0.0.0.0 --port 47950 --log-level info &",
      "UVICORN_PID=$!",
      "",
      ```
   c. Update the comment at the top of the block (was `"# --- Fast-start uvicorn"`) — actually the comment is now gone since we removed the uvicorn block. The new comment is in the insertion.
3. **REFACTOR**: Run all UT-4 through UT-6 and UT-10. Update RT-1 to assert `uvicorn_idx > ingest_idx`. Run `test_it2_script_passes_bash_syntax_check` to verify valid bash.

---

### T4: SIGTERM Trap and Lock/Complete Marker Positioning

**Design doc section**: Change 1 (SIGTERM trap position, lines 165-173)
**Spec reference**: REQ-5 (lines 220-235)

**Description**: Verify the SIGTERM trap position is correct after the reordering. The trap (line 432-433) must fire before uvicorn starts (which is now after bootstrap). Also verify that the lock is cleared and complete marker is written inside the bootstrap block (unchanged).

**Files to verify (no code changes expected if T3 done correctly)**:
- `src/agentalloy/install/subcommands/container_runtime.py` (lines 432-433, 480-484)

**Tests to write (TDD)**:
- UT-7: `test_sigterm_trap_before_uvicorn` — `trap 'kill` line index < `UVICORN_PID=$!` line index; trap covers both `OLLAMA_PID` and `UVICORN_PID`
- UT-11: `test_no_packs_complete_marker_written` — `touch "$COMPLETE"` is inside the `if [ "$BOOTSTRAP_NEEDED" ]` block
- UT-12: `test_no_packs_lock_cleared` — `rm -f "$LOCK"` is inside the bootstrap block, before `touch "$COMPLETE"`
- UT-19: `test_sigterm_traps_both_pids` — script contains `kill ${OLLAMA_PID:-} ${UVICORN_PID:-}`

**TDD instructions**:
1. **RED**: Write `test_sigterm_trap_before_uvicorn` that asserts the trap fires before uvicorn PID assignment. Run — verify it passes (the trap was already before uvicorn in the old code, and it stays before uvicorn in the new code since uvicorn moves after the trap).
2. **GREEN**: If it passes, write UT-11 and UT-12 to verify lock/complete marker positioning. These should also pass as-is since the bootstrap block content is unchanged.
3. **REFACTOR**: Add UT-19 as a regression guard.

---

### T5: Update Function Docstring

**Design doc section**: Change 4 (lines 237-293)
**Spec reference**: REQ-5 (lines 220-235)

**Description**: Update the `_build_entrypoint_script()` docstring (lines 317-338) to reflect the new bootstrap sequence. The key change is that uvicorn now starts AFTER all bootstrap steps (pack install + complete marker) instead of before pack ingest.

**Files to modify**:
- `src/agentalloy/install/subcommands/container_runtime.py` (lines 317-338)

**Tests to write (TDD)**:
- No unit test for docstring. This is a documentation change.
- Manual review: Read the new docstring and verify it accurately describes the 9-step sequence in the design doc (lines 268-293).

**TDD instructions**:
1. **RED**: N/A — docstring update.
2. **GREEN**: Replace the docstring (lines 317-338) with the new version from the design doc (lines 267-293). Key changes:
   - Title: "sequenced bootstrap + uvicorn" (was "fast-start + checkpointed")
   - Point 3: "Starts uvicorn in the background before pack ingest" → "Starts uvicorn AFTER all bootstrap steps to avoid LadybugDB lock conflicts"
   - Reordered the numbered list to reflect the new sequence (Ollama → migrations → packs → complete → uvicorn)
3. **REFACTOR**: N/A.

---

### T6: Update Existing Test Expectations (Regression)

**Design doc section**: Impact Analysis (lines 423-428)
**Test plan**: Regression Tests (RT-1, RT-2, RT-3)

**Description**: Update existing test expectations in `tests/install/test_container_runtime_readiness.py` to match the new script order.

**Files to modify**:
- `tests/install/test_container_runtime_readiness.py`
  - Line 36: Rename `test_ut10_uvicorn_starts_before_pack_ingest` → `test_ut10_uvicorn_starts_after_pack_ingest`
  - Line 43: Change assertion `uvicorn_idx < ingest_idx` → `uvicorn_idx > ingest_idx`
  - Line 90-95: Update `test_ec12_ec13_no_packs_path` — assert `Installing always-on packs` and `install-packs --non-interactive` instead of `No packs specified`

**Tests to write (TDD)**:
- RT-1: `test_ut10_uvicorn_starts_after_pack_ingest` — assert `uvicorn_idx > ingest_idx`
- RT-2: `test_ec12_ec13_no_packs_path` — assert `Installing always-on packs` in script
- RT-3: `test_entrypoint_skips_bootstrap_when_flag_exists` in `tests/test_container_edge_cases.py` — assert `complete_marker < uvicorn_start`

**TDD instructions**:
1. **RED**: Run `test_ut10_uvicorn_starts_before_pack_ingest` — it should FAIL because the assertion `uvicorn_idx < ingest_idx` is now wrong.
2. **GREEN**: Update the test:
   - Rename to `test_ut10_uvicorn_starts_after_pack_ingest`
   - Change assertion to `uvicorn_idx > ingest_idx`
   - Update comment to "uvicorn must start AFTER pack ingest (lock fix)"
3. **REFACTOR**: Update `test_ec12_ec13_no_packs_path` to assert the new empty-packs behavior. Run the full test class to verify no regressions.

---

### T7: Integration Tests (Script Execution)

**Design doc section**: Sequence Diagram (lines 295-347)
**Test plan**: Integration Tests (IT-1 through IT-6)

**Description**: Write integration tests that execute the generated bash script in a controlled environment with mocked binaries. These tests verify the actual script behavior, not just string matching.

**Files to create/modify**:
- `tests/install/test_container_runtime_readiness.py` — add IT-1 through IT-6
- `tests/test_container_edge_cases.py` — add IT-4 through IT-6 (checkpoint resume, stale lock, bootstrap complete skip)

**Tests to write (TDD)**:
- IT-1: `test_script_executes_cleanly` — generate script with `packs=""`, write to temp, run `bash /tmp/test-entrypoint.sh`, assert exit code 0
- IT-2: `test_no_uvicorn_during_bootstrap` — mock env with ollama/uv/agentalloy/uvicorn stubs, verify `.bootstrap-complete` created, no uvicorn called during bootstrap
- IT-3: `test_per_pack_install_in_script` — generate with `packs="core,documentation"`, verify both packs installed with checkpoints
- IT-4: `test_checkpoint_resume_skips_installed` — pre-populate `.bootstrap-checkpoints` with `core`, verify only `documentation` installed
- IT-5: `test_stale_lock_recovery` — create stale lock (>2h), verify script starts fresh
- IT-6: `test_bootstrap_already_complete` — create `.bootstrap-complete`, verify script skips bootstrap and starts uvicorn

**TDD instructions**:
1. **RED**: Write IT-1: generate script with `packs=""`, write to temp file, execute with `subprocess.run(["bash", script_path])`. Assert exit code 0. Run — it will fail because the script tries to run real ollama/uv/uvicorn commands.
2. **GREEN**: For IT-1, mock the external commands by putting them in a temp directory on PATH. Create stub scripts that exit 0. This requires setting up a mock PATH with:
   - `ollama` → `#!/bin/sh; exit 0`
   - `curl` → `#!/bin/sh; echo "HTTP/1.1 200 OK"` (for Ollama health check)
   - `uv` → `#!/bin/sh; exit 0` (for migrations)
   - `agentalloy` → `#!/bin/sh; exit 0` (for install-packs)
   - `uvicorn` → `#!/bin/sh; exit 0` (but we verify it's NOT called during bootstrap)
3. **REFACTOR**: Write IT-2 through IT-6 using the same mock environment pattern.

---

### T8: Edge Case, Readiness Polling, and Backward Compatibility Tests

**Design doc section**: Error Handling (lines 349-371), Performance (lines 373-392), Security (lines 394-411)
**Test plan**: Edge Cases (EC-1 through EC-9), Host Readiness (HR-1 through HR-3), Backward Compatibility (BC-1 through BC-4)

**Description**: Write the remaining edge case tests, host-side readiness polling tests, and backward compatibility tests.

**Files to create/modify**:
- `tests/test_container_edge_cases.py` — add EC-1 through EC-9, BC-1 through BC-4
- `tests/install/test_container_runtime_readiness.py` — add HR-1 through HR-3

**Tests to write (TDD)**:
- EC-1: `test_stale_lock_from_crashed_container` — stale lock + partial checkpoints → all packs re-installed
- EC-2: `test_partial_bootstrap_crash_mid_pack` — recent lock + partial checkpoints → resume from checkpoint
- EC-3: `test_empty_container_data_volume` — fresh empty APP_DIR → migrations + install-packs + complete
- EC-4: `test_existing_data_volume_from_prior_version` — `.bootstrap-complete` exists → skip bootstrap, start uvicorn
- EC-5: `test_corrupt_checkpoint_file` — invalid checkpoint content → treat as no checkpoints, install all packs
- EC-6: `test_sigterm_during_pack_install` — SIGTERM during pack install → trap fires, lock NOT removed
- EC-7: `test_multiple_container_restarts` — stale lock → fresh bootstrap → second restart skips bootstrap
- EC-8: `test_packs_flag_with_spaces_and_extra_commas` — `packs="core, ,documentation,  ,engineering"` → `PACK_LIST=(core documentation engineering)`, `TOTAL=3`
- EC-9: `test_packs_flag_with_special_characters` — pack names properly shell-quoted
- HR-1: `test_readiness_polling_handles_uvicorn_not_started` — mock URLError for first 3 calls, then ready → returns True
- HR-2: `test_readiness_polling_timeout` — always URLError → returns False after timeout
- HR-3: `test_readiness_polling_warming_up_then_ready` — warming_up (2x) then ready → returns True
- BC-1: `test_existing_container_bootstrap_complete` — existing volume with `.bootstrap-complete` → start uvicorn immediately
- BC-2: `test_existing_container_no_bootstrap_flag` — existing volume without flag → full bootstrap
- BC-3: `test_container_no_packs_installs_always_on` — fresh volume, no packs → always-on packs installed
- BC-4: `test_container_with_packs_installs_all` — fresh volume, explicit packs → per-pack install, no lock errors

**TDD instructions**:
1. **RED**: Write EC-4 (`test_existing_data_volume_from_prior_version`): generate script with `packs=""`, create `.bootstrap-complete` file in temp APP_DIR, execute script. Assert bootstrap is skipped (no ollama/migrations/install-packs calls) and uvicorn starts. Run — the script should detect `.bootstrap-complete` and skip bootstrap.
2. **GREEN**: Most of these tests should pass once the code changes from T1-T3 are in place. Write them as assertions on the generated script content (for EC-8, EC-9) and as execution tests with mocks (for EC-1 through EC-3, EC-5 through EC-7).
3. **REFACTOR**: Group tests into classes: `TestEdgeCases`, `TestReadinessPolling`, `TestBackwardCompatibility`. Ensure all tests use the mock environment pattern from IT-1.

---

## Test-to-Task Mapping

| Test ID | Test Name | Owning Task | Category |
|---------|-----------|-------------|----------|
| UT-1 | test_empty_packs_installs_always_on | T1 | Unit |
| UT-2 | test_empty_packs_no_per_pack_loop | T1 | Unit |
| UT-3 | test_nonempty_packs_uses_per_pack_loop | T2 | Unit |
| UT-4 | test_uvicorn_after_bootstrap_complete | T3 | Unit |
| UT-5 | test_uvicorn_not_before_pack_install | T3 | Unit |
| UT-6 | test_uvicorn_after_migrations | T3 | Unit |
| UT-7 | test_sigterm_trap_before_uvicorn | T4 | Unit |
| UT-8 | test_script_passes_bash_syntax_check | T1 (with T3) | Unit |
| UT-9 | test_empty_packs_branch_has_both_echo_and_command | T1 | Unit |
| UT-10 | test_uvicorn_start_comment_updated | T3 | Unit |
| UT-11 | test_no_packs_complete_marker_written | T4 | Unit |
| UT-12 | test_no_packs_lock_cleared | T4 | Unit |
| UT-13 | test_lock_at_start | T2 | Unit |
| UT-14 | test_atomic_progress_writes | T2 | Unit |
| UT-15 | test_checkpoints_after_each_pack | T2 | Unit |
| UT-16 | test_stale_lock_detection | T2 | Unit |
| UT-17 | test_checkpoint_resume | T2 | Unit |
| UT-18 | test_corrupt_checkpoints_treated_as_none | T2 | Unit |
| UT-19 | test_sigterm_traps_both_pids | T4 | Unit |
| UT-20 | test_entrypoint_permissions | T3 | Unit |
| IT-1 | test_script_executes_cleanly | T7 | Integration |
| IT-2 | test_no_uvicorn_during_bootstrap | T7 | Integration |
| IT-3 | test_per_pack_install_in_script | T7 | Integration |
| IT-4 | test_checkpoint_resume_skips_installed | T7 | Integration |
| IT-5 | test_stale_lock_recovery | T7 | Integration |
| IT-6 | test_bootstrap_already_complete | T7 | Integration |
| EC-1 | test_stale_lock_from_crashed_container | T8 | Edge Case |
| EC-2 | test_partial_bootstrap_crash_mid_pack | T8 | Edge Case |
| EC-3 | test_empty_container_data_volume | T8 | Edge Case |
| EC-4 | test_existing_data_volume_from_prior_version | T8 | Edge Case |
| EC-5 | test_corrupt_checkpoint_file | T8 | Edge Case |
| EC-6 | test_sigterm_during_pack_install | T8 | Edge Case |
| EC-7 | test_multiple_container_restarts | T8 | Edge Case |
| EC-8 | test_packs_flag_with_spaces_and_extra_commas | T8 | Edge Case |
| EC-9 | test_packs_flag_with_special_characters | T8 | Edge Case |
| HR-1 | test_readiness_polling_handles_uvicorn_not_started | T8 | Readiness |
| HR-2 | test_readiness_polling_timeout | T8 | Readiness |
| HR-3 | test_readiness_polling_warming_up_then_ready | T8 | Readiness |
| BC-1 | test_existing_container_bootstrap_complete | T8 | Backward Compat |
| BC-2 | test_existing_container_no_bootstrap_flag | T8 | Backward Compat |
| BC-3 | test_container_no_packs_installs_always_on | T8 | Backward Compat |
| BC-4 | test_container_with_packs_installs_all | T8 | Backward Compat |
| RT-1 | test_ut10_uvicorn_starts_after_pack_ingest | T6 | Regression |
| RT-2 | test_ec12_ec13_no_packs_path | T6 | Regression |
| RT-3 | test_entrypoint_skips_bootstrap_when_flag_exists | T6 | Regression |

**Total tests**: 45 (20 unit + 6 integration + 9 edge case + 3 readiness + 4 backward compat + 3 regression)

---

## Design Review Notes

### Concern 1: Increased Initial Wait Time (LOW RISK)
The design doc acknowledges this at lines 375-378. With always-on packs (5 packs), initial wait increases by ~2-5 minutes. The grace window of 1800s (30 min) in `_wait_for_readiness()` (line 631 of container_runtime.py) is sufficient. **Mitigation**: Acceptable. The 1800s timeout was already designed for full pack ingest.

### Concern 2: `/readiness` Endpoint Unavailable During Bootstrap (LOW RISK)
The design doc at lines 380-388 notes that `/readiness` is NOT reachable until bootstrap completes. The host-side `_wait_for_readiness()` already handles connection errors for up to 1800s. **Mitigation**: Acceptable. The readiness polling was already designed for this scenario.

### Concern 3: SIGTERM During Pack Install Kills Ollama
The SIGTERM trap at line 433 kills both `OLLAMA_PID` and `UVICORN_PID`. After the reordering, Ollama is started at line 411 and stays running throughout the entire entrypoint (including pack install and uvicorn). If SIGTERM fires during pack install, Ollama will be killed — which is correct behavior (clean shutdown). **No issue**.

### Concern 4: `--non-interactive` with Explicit `--packs`
When `--packs` is specified, the per-pack loop runs (not the `--non-interactive` install-packs call). The `--non-interactive` flag is only used when no packs are specified. This is correct per `_select_packs()` line 383-391: explicit `--packs` overrides the non-TTY default. **No issue**.

### Concern 5: Entrypoint Comment References Obsolete Behavior
The comment at lines 436-438 says "Start uvicorn BEFORE pack ingest so /readiness is reachable while bootstrap is in progress." This must be updated as part of T3. **Covered by T3**.

### Concern 6: Docstring Still Describes Old Behavior
The docstring at lines 317-338 says "Starts uvicorn in the background before pack ingest." This must be updated as part of T5. **Covered by T5**.

### Concern 7: No New Attack Surface
The design doc at lines 394-405 confirms no new external dependencies, network calls, or file paths. `agentalloy install-packs --non-interactive --no-restart` is the same command used in native setup. **No issue**.

### Overall Assessment
The design is sound. The changes are surgical (one function, three logical changes), low-risk, and well-documented. The main trade-off is the increased initial wait time, which is acceptable given the 1800s grace window. All existing safety mechanisms (stale lock recovery, checkpoint resume, SIGTERM trap, `set -e`) remain intact.

---

## Task Execution Order

```
T1  [Empty packs: replace skip with install-packs call]
T2  [Per-pack loop: verify unchanged + regression guards]
T3  [Uvicorn: move after bootstrap complete]
T4  [SIGTERM trap + lock/complete marker positioning]
T5  [Update function docstring]
T6  [Update existing test expectations (regression)]
T7  [Integration tests: script execution]
T8  [Edge case, readiness polling, backward compatibility tests]
```

T1-T4 can be done as individual Build passes (each small enough for one pass).
T5-T6 should be done together (docstring + test updates).
T7-T8 should be done together (all new test file content).

**Total Build passes**: 4 (T1, T2, T3/T4 combined, T5/T6, T7/T8)

---

## Phase Acceptance Criteria

### Phase 1 Acceptance (T1, T2)
- [ ] `agentalloy install-packs --non-interactive --no-restart` appears in script when `packs=""`
- [ ] "No packs specified - skipping pack installation" does NOT appear in script
- [ ] Per-pack loop still works correctly for non-empty `packs`
- [ ] All existing UT tests pass (UT-3 through UT-20)
- [ ] `bash -n` syntax check passes for all pack configurations

### Phase 2 Acceptance (T3, T4)
- [ ] `uv run uvicorn` appears AFTER `touch "$COMPLETE"` in script
- [ ] `uv run uvicorn` appears AFTER all pack install commands in script
- [ ] `echo ">> Starting uvicorn..."` comment updated (no "fast-start" or "before pack ingest")
- [ ] SIGTERM trap fires before uvicorn PID assignment
- [ ] Lock cleared and complete marker written inside bootstrap block
- [ ] `bash -n` syntax check passes

### Phase 3 Acceptance (T5, T6)
- [ ] Function docstring reflects new 9-step sequence
- [ ] Docstring mentions uvicorn starts AFTER pack installation
- [ ] `test_ut10_uvicorn_starts_after_pack_ingest` asserts `uvicorn_idx > ingest_idx`
- [ ] `test_ec12_ec13_no_packs_path` asserts `Installing always-on packs`
- [ ] All existing tests in `TestEntrypointScript` pass

### Phase 4 Acceptance (T7, T8)
- [ ] All 45 tests pass (20 unit + 6 integration + 9 edge case + 3 readiness + 4 backward compat + 3 regression)
- [ ] Integration tests verify actual script execution with mocked binaries
- [ ] Edge cases cover: stale lock, corrupt checkpoints, partial bootstrap, empty/existing volumes, SIGTERM during install
- [ ] Backward compatibility tests verify: existing container with `.bootstrap-complete`, existing container without flag, no packs, explicit packs

---

## Files Summary

### Files to Modify
1. `src/agentalloy/install/subcommands/container_runtime.py`
   - Lines 474-475: Replace empty-packs skip with install-packs call
   - Lines 435-441: Remove uvicorn start block (move to after bootstrap)
   - Lines 477-488: Insert uvicorn start block after bootstrap-complete
   - Lines 317-338: Update docstring

2. `tests/install/test_container_runtime_readiness.py`
   - Line 36-48: Update `test_ut10_uvicorn_starts_before_pack_ingest` → `test_ut10_uvicorn_starts_after_pack_ingest`
   - Lines 90-95: Update `test_ec12_ec13_no_packs_path`
   - Add: IT-1 through IT-6 integration tests
   - Add: HR-1 through HR-3 readiness polling tests

3. `tests/test_container_edge_cases.py`
   - Add: EC-1 through EC-9 edge case tests
   - Add: BC-1 through BC-4 backward compatibility tests
   - Add: RT-3 regression test

### Files to Read (for context, no changes)
- `src/agentalloy/install/subcommands/install_packs.py` (lines 365-408) — `_select_packs()` always-on default
- `src/agentalloy/install/subcommands/pull_models.py` (lines 152-189, 650-654) — Issue 1 (already resolved)
- `src/agentalloy/storage/vector_store.py` (lines 418-493) — Issue 2 (already resolved)

---

## Branch Strategy

```bash
# Create branch from main
git checkout main
git pull origin main
git checkout -b feature/test-cycle-fixes

# After each Build pass, commit and push
git add -A
git commit -m "T1: Empty packs — install always-on packs via install-packs --non-interactive"
git push origin feature/test-cycle-fixes

# Repeat for T2, T3, T4, T5, T6, T7, T8

# Run full test suite before PR
pytest tests/install/test_container_runtime_readiness.py -v
pytest tests/test_container_edge_cases.py -v
```

## Non-Goals (No Tasks)
- Issue 1 (Ollama SSH key): Already resolved in `pull_models.py`
- Issue 2 (FTS index rebuild): Already resolved in `vector_store.py`
- Native install pack flow: Already handles always-on packs correctly
- Root cause fix for DuckDB FTS stopwords bug: Upstream issue, workaround sufficient
- Container read-only DB mode: Would cause health check failures
