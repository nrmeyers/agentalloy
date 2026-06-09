# Test Case Redundancy Audit — Final Corrected

## CORRECTIONS FROM PREVIOUS VERSION

| Recommendation | Previous Verdict | Corrected Verdict | Reason |
|----------------|------------------|-------------------|--------|
| test_dispatcher.py -> test_phase_cli.py | Merge | **INVALID** | Different modules (dispatcher vs phase) |
| test_pack_tier_registry_consistency.py -> test_install_packs.py | Merge | **INVALID** | Different modules (pack_tier vs install_packs) |
| test_sidecar_watcher.py location | tests/ | **tests/install/** | Wrong path in previous report |

## VALIDATED RECOMMENDATIONS

### 1. Split `test_retrieval_with_contract.py` (10 tests, 232 lines)

This file tests three unrelated modules. Verified against targets:

| Tests to move | Count | Target | Overlap? |
|---------------|-------|--------|----------|
| `resolved_contract_tags_*` (4 tests) | 4 | `test_compose_contract.py` | **None.** test_compose_contract.py tests ComposeRequest but NOT `resolved_contract_tags`. Clean addition. |
| `_validate_skill_data` workflow tests (3 tests) | 3 | `test_customize.py` | **Non-overlapping.** test_customize.py tests missing exit_gates/applies_to_phases; these test missing contract_template and shipped-file smoke test. |
| `retrieve_domain_candidates` tests (3 tests) | 3 | New file `test_retrieval_contract.py` | Stays in retrieval scope. |

**Action:** Split into 3 files. No test count change, but proper module boundaries.

### 2. Merge `tests/test_state_migration.py` (1 test) -> `tests/install/test_state_migration.py` (9 tests)

Both test `agentalloy.install.state` migrations. Root file tests v3->v4, install file tests v3->v4, v4->v5, v5 passthrough. The v3->v4 test in the root file is a subset of what the install file already covers.

**Action:** Delete root file. The install file already has `test_v3_migrated_to_v4` and `test_v3_migrate_direct` covering the same ground.

### 3. Merge `tests/test_hook_router_fixes.py` (3 tests) -> `tests/test_hook_router_integration.py` (26 tests)

Both test `agentalloy.api.hook_router`. The fixes file tests specific edge cases (`_evaluate_sync` tool_name handling, pre-tool-use, user-prompt-submit). The integration file tests the full integration (hook script, caching, wiring). No test name overlap.

**Action:** Move the 3 tests into `test_hook_router_integration.py`.

### 4. Merge `tests/test_migrate_idempotent.py` (1 test) -> `tests/test_storage_ladybug.py` (5 tests)

Both test `agentalloy.storage.ladybug`. The migrate test verifies idempotent `migrate()` calls. The storage file tests storage operations. Same module, no overlap.

**Action:** Move the test into `test_storage_ladybug.py`.

### 5. Merge `tests/test_retrieval_workflow_class.py` (5 tests) -> `tests/test_reads_active_fragments.py` (9 tests)

Both test `agentalloy.reads.get_active_fragments()`. The reads file tests `skill_class="domain"` filter. The workflow file tests `skill_class="workflow"`, `skill_class="domain"` (exclusion), and tuple queries. The `test_domain_filter_excludes_workflow_fragments` test is a duplicate of `test_skill_class_filter_domain_only` — same invariant, different assertion style.

**Action:** Move 4 workflow-specific tests into `test_reads_active_fragments.py`. Delete `test_domain_filter_excludes_workflow_fragments` (covered by `test_skill_class_filter_domain_only`).

### 6. `tests/test_layout.py` (2 tests) — trivial import checks

Tests that `agentalloy` has `__version__` and `agentalloy.app.create_app` is callable. These are not testing behavior — they're checking package structure.

**Action:** Delete. These tests provide no regression protection and will only break when the package structure changes (which is a compile-time, not test-time, concern).

### NOT RECOMMENDED (my corrections)

| File | Why not merge |
|------|--------------|
| `test_dispatcher.py` | Tests `install.dispatcher` (subcommand registration), not `phase` CLI |
| `test_pack_tier_registry_consistency.py` | Tests `install.pack_tier` registry, not `install_packs` state |
| `test_sidecar_watcher.py` | Different location (`tests/install/` vs `tests/`), different concern (proxy wiring vs setup) |
| `test_jsx_stripping.py` | Tests shipped skill files for JSX artifacts, not validation logic |
| `test_governance_assembly.py` | Only file testing its module — keep standalone |

---

## FINAL ACTIONABLE SUMMARY

| Action | Files | Tests | Lines |
|--------|-------|-------|-------|
| Split retrieval_with_contract.py | 1 -> 3 | 10 | ~230 |
| Delete test_state_migration.py (root) | 1 | 1 | ~60 |
| Merge hook_router_fixes into integration | 1 | 3 | ~90 |
| Merge migrate_idempotent into storage_ladybug | 1 | 1 | ~30 |
| Merge workflow_class into reads_active_fragments | 1 | 4 | ~130 |
| Delete test_layout.py | 1 | 2 | ~15 |
| **Net** | **-3 files** | **+1** (net from split) | **-555** |

The net file count goes down by 3 (split creates +1, deletions -4). The test count stays roughly the same (net +1 from split). The line count drops by ~555 (~1.5% of total).

The real value is in removing the 6 micro-files that each have 1-2 tests and serve as single-purpose test stubs rather than meaningful test suites.
