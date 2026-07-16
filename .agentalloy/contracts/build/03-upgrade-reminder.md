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

Add `_knowledge_graph_enable_reminder` to `install/subcommands/upgrade.py`,
structured exactly like the existing `_code_index_enable_reminder` (same
one-line-tip shape reading `install_state.parse_env_file()`), and append its
result to the same `notices` list `upgrade_native` already builds alongside
the code-index reminder — firing only when `KNOWLEDGE_GRAPH_ENABLED` is
absent or false.

## Test cases

- AC 3: `agentalloy upgrade`'s summary includes the knowledge-graph reminder
  when the feature is off, and omits it when `KNOWLEDGE_GRAPH_ENABLED=True`.
- No regression to the existing code-index reminder.

## Plan

Approach + task order live in docs/design/knowledge-management-production/
(approach.md, tasks.md) — this task is item 3 of 3, not yet started.
