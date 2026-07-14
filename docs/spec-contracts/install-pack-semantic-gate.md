---
phase: spec
task_slug: install-pack-semantic-gate
route: full
domain_tags:
  - install-pack
  - skill-quality-gate
  - semantic-review
  - verdict-artifact
scope:
  touches:
    - "src/agentalloy/install/subcommands/install_pack.py"
    - "src/agentalloy/install/subcommands/validate_pack.py"
    - "src/agentalloy/pack_validation.py"
    - "src/agentalloy/web/wizard_api.py"
    - "src/agentalloy/_packs/meta/**"
    - "docs/spec/install-pack-semantic-gate.md"
    - "tests/**"
  avoids:
    - "src/agentalloy/authoring/**"
    - "src/agentalloy/api/**"
    - "src/agentalloy/orchestration/**"
    - "src/agentalloy/config.py"
    - "src/agentalloy/lm_client.py"
success_criteria: []
related_contracts:
  - "docs/install-pack-quality-gate-spec.md"
  - "docs/spec-contracts/compound-engineering-bridge.md"
created_at: 2026-07-13T00:00:00Z
---

# install-pack-semantic-gate

## Scope in a sentence

Add a **semantic** quality gate to `install-pack` (**Gate 1.5**, after the
deterministic schema+lint Gate 1) that enforces a per-skill review **verdict
artifact** produced by *the operator's own coding agent* — whatever LLM they are
already running — so a skill's *correctness*, not just its *well-formedness*, is
checked before it enters the corpus, **without putting any LLM in the install-pack
backend path** (the runtime stays deterministic and fully local; no revival of the
retired `authoring/` critic or its LM Studio config).

## Spec

Acceptance criteria and out-of-scope live in
`docs/spec-contracts/install-pack-semantic-gate.spec.md`.

> Runtime/tracked note: to arm a live SDD run, copy the spec doc to
> `docs/spec/install-pack-semantic-gate.md` (dropping the `.spec` infix so the spec
> exit-gate's `docs/spec/*.md` glob finds it) and this contract to
> `.agentalloy/contracts/spec/install-pack-semantic-gate.md` (git-ignored runtime
> path). The spec lays out a **sliced** delivery; design fans it into per-slice
> work rather than one build.
