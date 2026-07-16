---
phase: build
task_slug: 02-setup-wizard
work_item: knowledge-management-production
route: full
# domain_tags: 1-2 tags, ONE dominant tech surface — never every surface.
# Build retrieval is ~4 skills per contract; a 7-tag basket starves most
# surfaces (important fragments truncate, scores muddy). Keep it narrow.
domain_tags:
  - cli-subcommand
scope:
  touches:
    - "src/agentalloy/install/subcommands/simple_setup.py"
  avoids:
    - "src/agentalloy/install/subcommands/config.py"
    - "src/agentalloy/install/subcommands/upgrade.py"
success_criteria: []
related_contracts:
  - ".agentalloy/contracts/design/knowledge-management-production.md"
created_at: 2026-07-15T22:36:23Z
---

# 02-setup-wizard

## Task

**Revised from the original scope.** The original plan was to add
`"knowledge-graph"` as a *third, independent* selectable module. Investigation
before build found that's not architecturally real: `KNOWLEDGE_GRAPH_ENABLED`
never gated anything at runtime (not read by `app.py`, not in `MODULE_TOGGLES`,
not checked by `agentalloy knowledge why`) — Knowledge rides the same router
and store as `code_index` unconditionally. Adding a separate wizard option for
it would have shipped a second placebo toggle alongside the one already
removed from `config.py`/`env_forwarding.py` (see 01-config-cli's updated
status).

Closed instead by updating the *existing* `code-index` menu entry's label in
`install/subcommands/simple_setup.py` to say what it actually includes now:
"Codebase indexer (code search, call graphs & decision graph)" — no new
`_VALID_MODULES` entry, no new env var.

## Test cases

- Menu label mentions the decision graph.
- No regression to the existing `injector`/`code-index`/`both` selections or
  `_module_env_overrides`.

## Plan

Approach + task order live in docs/design/knowledge-management-production/
(approach.md, tasks.md) — this task is item 2 of 3. Done.
