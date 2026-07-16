---
phase: intake
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
    - "src/agentalloy/install/__main__.py"
    - "src/agentalloy/config.py"
    - "src/agentalloy/install/env_forwarding.py"
    - "tests/**"
  avoids:
    - "src/agentalloy/code_index/**"
    - "src/agentalloy/api/**"
success_criteria: []
related_contracts: []
created_at: 2026-07-15T22:36:11Z
---

# knowledge-management-production

## What the user actually wants

The Knowledge Management feature (code index + knowledge decision graph) has
already been validated locally (see the separate `knowledge-dogfooding`
work-item) and now needs to be productionized in the `agentalloy` CLI: a third
module option in the `agentalloy setup` wizard, `agentalloy config
enable/disable/status knowledge-graph` subcommands to toggle it post-install
without a full reinstall, and an `agentalloy upgrade` reminder when the
feature is available but off — mirroring the existing `code-index` module
toggle end-to-end.

## Intent signals

- intent: new-build
- artifact_type: feature
- scope: medium — touches the setup wizard, a new config subcommand group, and
  the upgrade reminder; avoids the code-index/knowledge-graph engines
  themselves (already built and validated)
- urgency: soon

## Proposed route

full — this is real product surface (wizard + CLI + upgrade UX) with several
independent pieces; each gets its own build contract (see design's task
breakdown) rather than one whole-feature build.
