---
phase: design
task_slug: install-pack-semantic-gate
route: full
domain_tags:
  - install-pack
  - skill-quality-gate
  - verdict-artifact
  - pack-validation
  - cli-subcommand
scope:
  touches:
    - "src/agentalloy/pack_validation.py"
    - "src/agentalloy/install/subcommands/install_pack.py"
    - "src/agentalloy/install/subcommands/validate_pack.py"
    - "docs/**"
    - "tests/**"
  avoids:
    - "src/agentalloy/api/**"
    - "src/agentalloy/orchestration/**"
    - "src/agentalloy/config.py"
    - "src/agentalloy/lm_client.py"
    - "src/agentalloy/web/wizard_api.py"
    - "src/agentalloy/_packs/**"
success_criteria: []
related_contracts:
  - "docs/spec-contracts/install-pack-semantic-gate.spec.md"
created_at: 2026-07-13T00:00:00Z
---

# install-pack-semantic-gate

## Scope in a sentence

Design **slice 1** of the semantic gate — the deterministic **Gate 1.5** that
enforces an agent-authored `review.yaml` verdict at `install-pack` (schema +
validator in `pack_validation.py`, wiring in `install_pack.py` after Gate 1 and
before the version gate, dry-run parity in `validate_pack.py`, and the
`--allow-unreviewed` escape hatch) — resolving the spec's eight open "how"
decisions (DK1–DK8) against real code, **without** calling an LLM, touching the
serving runtime, or building the review workflow (slice 2) or web surfacing
(slice 3).

## Design

Approach, task plan, and test cases live in the
`install-pack-semantic-gate.design/` folder (`approach.md`, `tasks.md`,
`test-plan.md`). Acceptance is fixed by
`docs/spec-contracts/install-pack-semantic-gate.spec.md` (`## Acceptance
Criteria`, AC 1–10) and is **not** reopened here; tasks name the `AC-N` they
satisfy. This design covers **slice 1 only** — AC 1–6, the CLI half of AC 8,
and AC 9–10. AC 7 (the review workflow) is **slice 2** and AC 8's web-lane
surfacing + optional class-scoped independence is **slice 3**; both are out of
scope for this design (`_packs/**` and `web/wizard_api.py` are in `avoids`).

> **Open review item (product decision — DK6).** The CLI `install-pack` path has
> no human `run_approve` step (that exists only in the web lane). This design
> defaults the CLI posture to **process-forcing + auditable** (the operator who
> runs the command is the approver; the gate guarantees a review ran and is
> recorded), with independence-claim enforcement available via config. The
> alternative — reject `mode: self` on the CLI — is documented in `approach.md`.
> This is the one decision that changes the product's guarantee; confirm at
> design review before build.

> Location note. At runtime this contract's home is
> `.agentalloy/contracts/design/install-pack-semantic-gate.md` and the design
> docs' is `docs/design/install-pack-semantic-gate/` — both git-ignored SDD
> runtime paths. The committed, reviewable copies live under `docs/spec-contracts/`:
> this contract, and the `install-pack-semantic-gate.design/` folder. The
> per-task build contracts (design→build hand-off) will land in
> `install-pack-semantic-gate.build/` as slice 1 is built.
