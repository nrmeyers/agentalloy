# Cycle-end sweep: broadening the archive trigger

## Problem

The cycle-end archive sweep only fired on `ship → intake`. Work items resetting from build, qa, design, or spec left all contracts and artifacts permanently `'active'` in the DuckDB state store, causing gates to grade all history and approval checks to fail on edited shipped artifacts.

## Approach that worked

1. **CLI trigger:** Changed the single boolean condition from `current == "ship" and phase == "intake"` to `current is not None and current != "intake" and phase == "intake"`. This widens the trigger to any non-intake phase → intake transition while remaining idempotent for `intake → intake`.

2. **Proxy path:** Added the same archive-on-intake block in `_write_phase_atomic()` so the proxy auto-advance path mirrors the CLI behavior. Uses `view.archive_all()` directly (in-process store handle), not `StateClient`.

3. **Tests:** Added 6 integration tests covering all new trigger paths. Existing tests still pass.

## What didn't work (and why)

Initially, the proxy path used `StateClient` (the HTTP client wrapper) instead of the in-process `view` store handle. This violated the test `test_signals_module_no_state_client_import`, which enforces that in-process paths in `agentalloy.signals` must use `StateStore` directly. The fix was to switch to `view.archive_all()` — the store handle is already available in `_write_phase_atomic()` via `_phase_view()`.

Additionally, the test does a literal string search for `"StateClient"` in source files, so even comments mentioning the class name caused failures. Had to remove the literal from a comment.

## Key decisions

- **`current is not None` guard:** Prevents archiving on first-time intake set (repo with no stored phase row). `current == "ship"` would have worked too since `None == "ship"` is `False`, but `current is not None` is more explicit about the intent.
- **`current != "intake"` guard:** Idempotent — `intake → intake` must not re-archive.
- **Store handle vs HTTP client:** In-process paths use the store handle directly. HTTP clients (`StateClient`) are for cross-process communication. Mixing them in the proxy path caused a test violation.
- **Same logic, different call site:** CLI and proxy paths share identical semantics but are in different modules. Keeping them separate avoids circular imports and keeps each module's dependency graph clean.
