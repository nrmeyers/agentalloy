---
phase: build
task_slug: 01-config-cli
work_item: knowledge-management-production
route: full
# domain_tags: 1-2 tags, ONE dominant tech surface — never every surface.
# Build retrieval is ~4 skills per contract; a 7-tag basket starves most
# surfaces (important fragments truncate, scores muddy). Keep it narrow.
domain_tags:
  - cli-subcommand
scope:
  touches:
    - "src/agentalloy/install/subcommands/config.py"
    - "src/agentalloy/config.py"
    - "src/agentalloy/install/env_forwarding.py"
    - "src/agentalloy/install/__main__.py"
  avoids:
    - "src/agentalloy/install/subcommands/simple_setup.py"
    - "src/agentalloy/install/subcommands/upgrade.py"
success_criteria: []
related_contracts:
  - ".agentalloy/contracts/design/knowledge-management-production.md"
created_at: 2026-07-15T22:36:20Z
---

# 01-config-cli

## Task

Implement `agentalloy config status|enable|disable code-index` in
`install/subcommands/config.py`, wired into `install/__main__.py`'s
subparser registry, persisted to the user-scoped `.env` via the existing
`install_state` parse/upsert-env-file helpers. `enable`/`disable`/`status`
are the accepted verbs (not the original ask's `start`/`stop` — see design's
approach.md); no foreground service process is started or stopped, only the
persistent `.env` flag.

**Correction (post-ship): `knowledge-graph` is not a separate feature here.**
The original build added a `knowledge_graph_enabled` Settings field and a
`knowledge-graph` entry in `_FEATURE_TO_ENV`, but nothing in the runtime ever
read it — not `app.py`'s module registration, not `MODULE_TOGGLES`, not
`agentalloy knowledge why` (gated by `CODE_INDEX_ENABLED` alone via
`_guard_module`). It was a placebo toggle. Removed: the Settings field, its
`INTENT_KEYS` entry, and the `_FEATURE_TO_ENV` entry — `code-index` is now the
only feature `config` manages, and it genuinely covers Knowledge too (same
router, same store, no independent gate). See 02/03 for how the wizard/
upgrade-reminder pieces were closed consistently with this.

## Test cases

- `agentalloy config enable code-index` sets `CODE_INDEX_ENABLED=True` in
  `.env`; `disable` sets `False`; `status` reports current state; other
  `.env` keys are untouched.
- `test_settings_keys_all_classified` passes with no `KNOWLEDGE_GRAPH_ENABLED`
  key at all (field removed, not merely classified).
- `enable knowledge-graph` is rejected as an invalid `argparse` choice.

## Plan

Approach + task order live in docs/design/knowledge-management-production/
(approach.md, tasks.md) — this task is item 1 of 3.

## Status

Done. Shipped: the `config` subcommand group (`status`/`enable`/`disable`,
wired into `install/__main__.py`'s `_SUBCOMMANDS` — an earlier pass had
imported the module but never registered it, so `agentalloy config` silently
failed to parse), and `tests/install/test_config.py` (10 cases: status,
enable/disable, idempotency, comment/unrelated-key preservation, `.env`
permissions, a regression guard on the CLI-wiring bug, and a guard that
`knowledge-graph` is rejected). The `.env` write path was also moved off an
ad-hoc full-file regeneration (which discarded comments/ordering) onto the
shared, comment-preserving `install.state.upsert_env_file` the web
`/api/config` endpoint already used — both surfaces now share one
implementation. The placebo `knowledge_graph_enabled` Settings field/toggle
(added in the original pass, removed after the fact once it was found to
gate nothing) is gone.
