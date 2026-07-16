---
phase: build
task_slug: 03-upgrade-reminder
work_item: knowledge-management-production
route: full
# domain_tags: 1-2 tags, ONE dominant tech surface — never every surface.
# Build retrieval is ~4 skills per contract; a 7-tag basket starves most
# surfaces (important fragments truncate, scores muddy). Keep it narrow.
domain_tags:
  - cli-subcommand
scope:
  touches:
    - "src/agentalloy/install/subcommands/upgrade.py"
  avoids:
    - "src/agentalloy/install/subcommands/config.py"
    - "src/agentalloy/install/subcommands/simple_setup.py"
success_criteria: []
related_contracts:
  - ".agentalloy/contracts/design/knowledge-management-production.md"
created_at: 2026-07-15T22:36:26Z
---

# 03-upgrade-reminder

## Task

**Revised from the original scope**, same reason as 02-setup-wizard: a
separate `_knowledge_graph_enable_reminder` firing on `KNOWLEDGE_GRAPH_ENABLED`
would remind users to flip a flag that does nothing. Closed instead by
extending the existing `_code_index_enable_reminder` in
`install/subcommands/upgrade.py` to mention the decision graph in its one-line
tip — no new function, no new env var, no new `notices` entry.

## Test cases

- The existing code-index-off reminder text mentions `agentalloy knowledge
  why`/the decision graph.
- No regression to the reminder's on/off firing condition
  (`CODE_INDEX_ENABLED` truthy → `None`).

## Plan

Approach + task order live in docs/design/knowledge-management-production/
(approach.md, tasks.md) — this task is item 3 of 3. Done.
