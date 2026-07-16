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

Implement `agentalloy config status|enable|disable knowledge-graph` (and
`code-index`, already covered) in `install/subcommands/config.py`, wired into
`install/__main__.py`'s subparser registry, backed by the
`knowledge_graph_enabled: bool` field on `Settings` (`config.py`) persisted to
the user-scoped `.env` via the existing `install_state`
parse/upsert-env-file helpers — the exact shape already shipped for
`code-index`. `enable`/`disable`/`status` are the accepted verbs (not the
original ask's `start`/`stop` — see design's approach.md); no foreground
service process is started or stopped, only the persistent `.env` flag.

## Test cases

- AC 2: `agentalloy config enable knowledge-graph` sets
  `KNOWLEDGE_GRAPH_ENABLED=True` in `.env`; `disable` sets `False`; `status`
  reports current state; other `.env` keys are untouched.
- AC 4: `test_settings_keys_all_classified` passes — `KNOWLEDGE_GRAPH_ENABLED`
  is classified in `env_forwarding.INTENT_KEYS`.

## Plan

Approach + task order live in docs/design/knowledge-management-production/
(approach.md, tasks.md) — this task is item 1 of 3.

## Status

Shipped: the `config` subcommand group (`status`/`enable`/`disable`), the
`knowledge_graph_enabled` Settings field, and its `INTENT_KEYS`
classification. Remaining before this task closes: a dedicated
`tests/test_config.py` for the subcommand (currently only covered
transitively via `test_env_forwarding.py`).
