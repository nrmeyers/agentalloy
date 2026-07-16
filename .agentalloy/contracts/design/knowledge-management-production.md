---
phase: design
task_slug: knowledge-management-production
route: full
domain_tags:
  - cli-subcommand
  - config-management
  - install-wizard
scope:
  touches:
    - "src/agentalloy/install/subcommands/config.py"
    - "src/agentalloy/install/subcommands/simple_setup.py"
    - "src/agentalloy/install/subcommands/upgrade.py"
    - "src/agentalloy/install/env_forwarding.py"
    - "tests/**"
  avoids:
    - "src/agentalloy/code_index/**"
    - "src/agentalloy/api/**"
success_criteria: []
related_contracts:
  - ".agentalloy/contracts/spec/knowledge-management-production.md"
created_at: 2026-07-15T22:36:17Z
---

# knowledge-management-production

## Scope in a sentence

Three independent pieces of install/config UX wired onto the existing
`code-index` module-toggle pattern: a wizard option, a config subcommand
group, and an upgrade reminder. No engine changes.

## Design

Approach, task plan, and test cases live in docs/design/knowledge-management-production/
(approach.md, tasks.md, test-plan.md).
