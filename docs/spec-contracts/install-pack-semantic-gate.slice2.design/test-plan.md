# Install-Pack Semantic Gate — Slice 2 — Test Plan

Slice 2 is content (a meta skill + a workflow edit). It is verified by a
**contract-fidelity** test that binds the producer's prescription to slice 1's
frozen validator — no LLM, no network, deterministic in CI. Maps AC 7.

## § Producer ↔ validator fidelity (`tests/install/test_review_producer_fidelity.py`)

| Case | Asserts | AC / DK |
|------|---------|---------|
| golden verdict accepted | a `review.yaml` authored to `sys-skill-review-verdict`'s prescription passes the real `validate_review_verdicts` | AC 7; DK14 |
| coverage is complete | golden `checks` keys == full R1–R9 id set parsed from `sys-skill-authoring-rules.md` | AC 7; DK10, DK11 |
| partial map is a producer failure, not a gate failure | a checks map missing one rule **still** validates at the gate but **fails** the coverage assertion | DK4, DK10 |
| hash freshness holds | golden `target_hash` == `sha256:` of the fixture skill bytes; editing the fixture one byte breaks validation | AC 2 (regression); DK12 |
| all-`na` is *not* the intended shape | an all-`na` map validates at the gate (documents the known gap) but is called out as the rubber-stamp the producer prose forbids | DK10 |
| no LLM / network | `sys-skill-review-verdict.md` source contains no LM endpoint; test imports nothing from `lm_client`/`authoring` | AC 5 (regression) |

## § Meta-skill well-formedness

| Case | Asserts |
|------|---------|
| parser accepts the new skill | `skill_md.parser.parse_file("sys-skill-review-verdict.md")` succeeds (required header fields present) — run at build orientation; add a guard if a bundled-meta-parse test exists |
| no "review YAML" collision | the producer source never calls the *skill* YAML a "review YAML"; the phrase `review.yaml` always means the verdict artifact (grep guard) |

## § Workflow wiring

| Case | Asserts |
|------|---------|
| step 3 references the producer | `sdd-add-skill.yaml` `raw_prose` mentions `sys-skill-review-verdict` and `review.yaml` |
| sdd pack version bumped | `sdd/pack.yaml` version > its pre-edit value (propagation guard) |
| existing exit gates intact | `sdd-add-skill` `exit_gates` / `prose_invariants` unchanged except the additions (no regression to the approval checkpoint) |

## Explicitly not tested here

- A model actually producing the verdict end-to-end (non-deterministic; out of scope
  — AC 7 is satisfied by the fidelity contract, not a live model run).
- Gate 1.5 enforcement behaviour — slice 1's suite owns it; untouched.
- Corpus retrieval of the new skill (requires the shipped-corpus rebuild, a ship
  step outside this slice).
