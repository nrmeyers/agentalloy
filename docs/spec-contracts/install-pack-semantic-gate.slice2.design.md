---
phase: design
task_slug: install-pack-semantic-gate-slice2
route: full
related_spec: docs/spec-contracts/install-pack-semantic-gate.spec.md
domain_tags:
  - skill-pack-authoring
  - yaml-schema
  - meta-skill
  - workflow-gates
scope:
  touches:
    - "src/agentalloy/_packs/meta/sys-skill-review-verdict.md"   # NEW producer skill
    - "src/agentalloy/_packs/sdd/sdd-add-skill.yaml"             # wire step 3 → emit review.yaml
    - "tests/**"                                                  # AC-7 fidelity + fixture
    - "docs/**"
  avoids:
    - "src/agentalloy/pack_validation.py"      # slice 1, shipped — validator is frozen
    - "src/agentalloy/install/subcommands/**"  # slice 1 wiring is done
    - "src/agentalloy/api/**"
    - "src/agentalloy/orchestration/**"
    - "src/agentalloy/retrieval/**"
    - "src/agentalloy/config.py"
success_criteria: []
related_contracts:
  - docs/spec-contracts/install-pack-semantic-gate.design.md   # slice 1
created_at: 2026-07-14T00:00:00Z
---

# Install-Pack Semantic Gate — Slice 2 — The Review Producer

## Scope in a sentence

Author the **producer** half of the gate: a meta-pack skill that turns the R1–R9
authoring-rule evaluation into the exact machine-checkable `review.yaml` verdict
artifact slice 1's Gate 1.5 enforces — closing the loop between "the agent
reviewed the skill" and "the backend can prove a fresh, approving verdict exists."
Closes spec **AC 7**. No backend change: slice 1's validator is frozen.

## Contingency (read first — one review surface)

This slice is **contingent on the user accepting the slice-1 posture** that shipped
but was flagged for sign-off, and adds no new silently-resolved-but-flagged item:

- **Dormant default** — the gate ships behind `AGENTALLOY_INSTALL_REQUIRE_REVIEW`
  (off). Slice 2 is what makes the flag *safe to eventually turn on*; it does not
  turn it on.
- **DK6 posture** — CLI installs are process-forcing + auditable (`mode: self`
  allowed by default; independence is the `AGENTALLOY_INSTALL_REQUIRE_INDEPENDENT_REVIEW`
  lever). The producer emits `mode` honestly; it does not decide the posture.

If either is rejected at review, this slice's producer text changes but its shape
does not.

## Design

Approach, task plan, and test cases live in the `install-pack-semantic-gate.slice2.design/`
folder (`approach.md`, `tasks.md`, `test-plan.md`), resolving slice-2 decisions as
**DK9–DK14**. Acceptance is fixed by the parent spec (AC 7) and not reopened.
Per-task build contracts (design→build hand-off) are in
`install-pack-semantic-gate.slice2.build/`.

## The honest scope boundary (what "done" costs)

Authoring the producer `.md` is a **corpus content change**, not a pure file drop.
Meta `.md` skills are served corpus skills: `bootstrap.py` parses each via
`skill_md/parser.py` and inserts it into the DuckDB store. So for
`sys-skill-review-verdict` to be *retrievable* by the operator's agent, the shipped
corpus must be **rebuilt + re-embedded**, and the wheel/image **version-bumped**
(shipped-surface rule) — a ship step, done at release, not in this design. The
build contracts author the source + the fidelity test; they do **not** perform the
rebuild or flip the flag.
