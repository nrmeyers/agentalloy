---
phase: spec
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
    - "docs/spec/knowledge-management-production.md"
    - "tests/**"
  avoids:
    - "src/agentalloy/code_index/**"
    - "src/agentalloy/api/**"
success_criteria: []
related_contracts:
  - ".agentalloy/contracts/intake/knowledge-management-production.md"
created_at: 2026-07-15T22:36:14Z
---

# knowledge-management-production

## Scope in a sentence

Give the already-validated Knowledge Management feature (code index +
knowledge decision graph) the same install/config/upgrade plumbing the
`code-index` module already has: a wizard option, an enable/disable/status CLI
group, and an upgrade-time reminder — no changes to the feature's engines
themselves.

## Spec

Acceptance criteria and out-of-scope live in docs/spec/knowledge-management-production.md.
