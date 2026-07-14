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

## The honest scope boundary (verified at build — corrected from the pre-build guess)

The pre-build design guessed the producer was a *retrieval-corpus* change requiring
a rebuild + re-embed. **Build orientation proved otherwise.** Verified facts:

- `meta/` and `conventions/` have **no `pack.yaml`**; `_discover_packs` only finds
  `*/pack.yaml`, and neither appears in `agentalloy install-packs --list`. The CI
  corpus builder is `install-packs --packs all` (see `.github/workflows/corpus-nightly.yml`,
  `container-build.yml`) — so **the 5 existing meta skills are NOT in the served
  retrieval corpus**, and neither is the new one. No in-repo script bootstraps
  `_packs/meta/*.md` into DuckDB.
- Meta `.md` skills ship in the **wheel** as authoring/reference material, referenced
  **by name** from retrieved workflow skills (e.g. `sdd-add-skill` names
  `sys-skill-authoring-rules` and now `sys-skill-review-verdict`).

So `sys-skill-review-verdict` is authored as a **structural peer** of the 5 working
meta skills (same header shape; passes `bootstrap.parse_file` **and**
`bootstrap._validate`, asserted in the fidelity test) and reaches the agent by
**whatever path its siblings use** — no corpus rebuild required, no separate wheel
bump beyond the unreleased 6.12.0 the `_packs/**` edits ride.

**One open question (flagged, not silently resolved):** the exact delivery path from
`_packs/meta/*.md` to a *running* add-skill agent — reference-doc read vs a
bootstrap step in out-of-repo release tooling vs the separate `agentalloy-authoring`
package — is **not determinable from this repo**. It does not block slice 2 (the
producer is byte-for-pattern identical to its working siblings), but if the siblings
turn out to need an explicit enumeration entry somewhere in release tooling, the new
skill needs the same entry. Confirm the meta-skill delivery mechanism before relying
on the producer in a live add-skill session.
