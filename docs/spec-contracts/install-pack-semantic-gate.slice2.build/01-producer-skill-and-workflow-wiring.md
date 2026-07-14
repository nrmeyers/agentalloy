# Build 01 — Producer meta skill + `sdd-add-skill` wiring

**Implements:** tasks T1, T2 · **Satisfies:** AC 7 · **DKs:** DK9, DK10, DK11, DK12, DK13
**Design:** `install-pack-semantic-gate.slice2.design/approach.md`

## Do

1. **Create** `src/agentalloy/_packs/meta/sys-skill-review-verdict.md`.
   - Header block matching sibling meta skills — **first, at build orientation, read
     `src/agentalloy/skill_md/parser.py` and confirm the required header fields**
     (`skill_id`, `category`, `always_apply`, `phase_scope`, `category_scope`,
     `author`, `change_summary`) and reproduce them exactly. `skill_id:
     sys-skill-review-verdict`, `category: tooling`.
   - Prose sections in the T1 order: purpose + **`review.yaml`-vs-"review YAML"
     disambiguation** (DK9); the exact slice-1 verdict schema (DK9); the R1–R9
     per-rule coverage bar with defended `na`, keyed off `sys-skill-authoring-rules`,
     count not hardcoded (DK10, DK11); the hash one-liner run *after* read-only
     validate (DK12); `reviewer` honesty incl. the `independent`-claim warning (DK13).
   - The schema reproduced MUST match what slice 1's `validate_review_verdicts`
     accepts. Ground it against the frozen validator, not memory.

2. **Edit** `src/agentalloy/_packs/sdd/sdd-add-skill.yaml`:
   - `raw_prose` step 3: keep the per-rule verdict discipline; add "**and emit
     `review.yaml`** in the pack dir per `sys-skill-review-verdict`."
   - Step 4/5: one sentence that, when `AGENTALLOY_INSTALL_REQUIRE_REVIEW` is on,
     install requires the verdict; `--allow-unreviewed` is the loud bypass.
   - **Bump `src/agentalloy/_packs/sdd/pack.yaml` version** (propagation guard).
   - Do NOT change `exit_gates`; do NOT remove existing `prose_invariants`.

## Verify

- `uv run ruff check` clean; the two files parse (YAML + `skill_md.parser.parse_file`
  on the new `.md`).
- Grep guard: the new source contains no LM endpoint / `lm_client` / `authoring`
  reference, and never calls the *skill* YAML a "review YAML".

## Do NOT

- Touch `pack_validation.py` or install/validate wiring (slice 1, frozen).
- Rebuild/re-embed the corpus or bump the **wheel** version (ship step, not here).
- Flip `AGENTALLOY_INSTALL_REQUIRE_REVIEW`.
