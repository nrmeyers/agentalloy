---
phase: design
task_slug: compound-engineering-bridge
route: full
domain_tags:
  - python
  - deterministic-predicates
  - workflow-gates
  - cli-subcommand
  - skill-pack-authoring
  - yaml-schema
scope:
  touches:
    - "src/agentalloy/_packs/sdd/sdd-verify-and-review.yaml"
    - "src/agentalloy/signals/predicates.py"
    - "src/agentalloy/api/proxy_signal.py"
    - "src/agentalloy/contracts.py"
    - "src/agentalloy/install/subcommands/**"
    - "docs/solutions/**"
    - "tests/**"
  avoids:
    - "src/agentalloy/code_index/**"
    - "src/agentalloy/retrieval/**"
    - "src/agentalloy/api/**"
    - "src/agentalloy/_corpus/**"
success_criteria: []
related_contracts: []
created_at: 2026-07-08T00:00:00Z
---

# compound-engineering-bridge

## Scope in a sentence

Design the two pieces the spec settled — a per-task codify gate at ship, and a
lesson→corpus promotion path — resolving the five open decisions the spec left
to design (gate predicate, gated edge, `--force`, promotion-flow shape,
duplicate handling), without touching the read-path or proxy surfaces.

## Design

Approach, task plan, and test cases live in `docs/design/compound-engineering-bridge/`
(`approach.md`, `tasks.md`, `test-plan.md`). Acceptance is fixed by
`docs/spec/compound-engineering-bridge.md` and is not reopened here; tasks name
the `AC-N` they satisfy.

> Location note. At runtime this contract's home is
> `.agentalloy/contracts/design/compound-engineering-bridge.md` and the design
> docs' is `docs/design/compound-engineering-bridge/` — both git-ignored SDD
> runtime paths. The committed, reviewable copies live under `docs/spec-contracts/`:
> this contract, and the `compound-engineering-bridge.design/` folder for the
> three design docs. The per-task build contracts (design→build hand-off) are in
> `compound-engineering-bridge.build/`.
