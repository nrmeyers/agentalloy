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

Add `"knowledge-graph"` as a third selectable module in
`install/subcommands/simple_setup.py`'s interactive wizard: extend
`_VALID_MODULES` (currently `{"injector", "code-index", "both"}`) and the
menu built around it, and extend `_module_env_overrides` so selecting it
emits `KNOWLEDGE_GRAPH_ENABLED=1` (plus `CODE_INDEX_ENABLED=1`, since the
knowledge graph builds on the code index) to the generated `.env` lines.

## Test cases

- AC 1: `agentalloy setup`'s module menu lists `knowledge-graph`; selecting it
  writes `KNOWLEDGE_GRAPH_ENABLED=1` (and `CODE_INDEX_ENABLED=1`) to `.env`.
- No regression to the existing `injector`/`code-index`/`both` selections.

## Plan

Approach + task order live in docs/design/knowledge-management-production/
(approach.md, tasks.md) — this task is item 2 of 3, not yet started.
