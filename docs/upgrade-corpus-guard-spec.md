# Spec: mirror the empty-corpus guard onto the `upgrade` path

> **Status: IMPLEMENTED.** This spec describes a shipped feature; retained as design rationale. Cited modules moved to `install/subcommands/`; paths below are updated accordingly.

## Problem

#261 added a post-`install-packs` guard to `setup` (`run_setup`): if the corpus
didn't actually populate (`< MIN_SKILL_COUNT` embedded skills), setup fails loudly
instead of reporting a half-install as done. `upgrade` re-implements its own step
sequence and never inherited that check, so it has the **same blind spot**:

`_upgrade_native` (`install/subcommands/upgrade.py:251`) runs `install-packs`, and only re-embeds
on an embedding-**dimension** mismatch. If `install-packs` silently leaves an
empty/partial corpus *without* a dim change, upgrade logs "re-ingested packs" and
restarts — a silent half-upgrade.

(The reconcile half of #261 — detect/overwrite a prior install, native↔container
switch — is setup-time by nature and intentionally out of scope here.)

## Design

Extract the corpus-count check into one shared helper and call it from both paths.

1. **`seed_corpus.py` — new public `corpus_skill_count() -> int`.** Moves the body
   of `simple_setup._corpus_skill_count` to the module that already owns
   `MIN_SKILL_COUNT` + `_check_duckdb`: resolve `install_state.corpus_dir()`,
   require `agentalloy.duck` present (the v5 skill store; its companion vector
   index is `fragments.lance`), return `_check_duckdb(duck).skill_count` (or `0`
   on absent/empty/unreadable). Never raises.

2. **`simple_setup._corpus_skill_count` → delegate.** Keep the function name (it's
   the patchable seam the #261 tests stub) but have it return
   `seed_corpus.corpus_skill_count()`. No behavior change for setup.

3. **`upgrade._upgrade_native` — add the guard.** After the ingest / conditional
   re-embed block (post line ~302, before the `update` migrations + restart), if
   `seed_corpus.corpus_skill_count() < seed_corpus.MIN_SKILL_COUNT`, append a loud
   warning naming the count + remediation (`reembed --force` / `doctor`). A
   warning makes `_run` return exit 1 (`install/subcommands/upgrade.py:664` returns `1 if warnings else 0`),
   so the half-upgrade surfaces as a non-clean status. Still restart the service
   afterward (don't strand it), but the run reports failure.

   Container path is unchanged: the image ships a prebuilt, verified corpus and
   self-heals on entry, so the host can't meaningfully assert it here.

## Tests

- `seed_corpus.corpus_skill_count`: returns the count for a populated corpus
  (patch `_check_duckdb`); `0` when files absent / `_check_duckdb` raises.
- `upgrade._upgrade_native`: with the swap/ingest mocked and
  `seed_corpus.corpus_skill_count` patched to `0` → warnings contain the
  empty-corpus message; patched to `>= MIN_SKILL_COUNT` → no such warning.
- Existing `simple_setup` #261 tests still pass (seam name preserved).

## Verify

`pytest tests/install/test_upgrade.py tests/test_simple_setup.py tests/install/test_pull_models.py -q`
+ ruff format/check + pyright (0 errors).
