# Install-Pack Semantic Gate — Slice 2 — Task Plan

Each task names the AC it satisfies (parent spec) and the DK it implements. Ordered
so the producer skill exists before the workflow references it and before the test
fixture is authored against it.

## T1 — Author the producer meta skill `sys-skill-review-verdict.md`  *(AC 7; DK9, DK10, DK11, DK12, DK13)*

New file `src/agentalloy/_packs/meta/sys-skill-review-verdict.md`, structured like
its sibling meta skills (`sys-skill-transform-contract.md` header block + prose).
Content, in order:

1. **Purpose + disambiguation** — "emit the **verdict** artifact `review.yaml` that
   install-pack's Gate 1.5 enforces." State on first use that this is *not* the
   "review YAML" of `sys-skill-transform-contract` (that's the skill YAML). (DK9)
2. **The exact schema** — reproduce the slice-1 `review.yaml` shape verbatim
   (`schema_version`, `reviews[]` with `skill_id`, `target_hash`, `verdict`,
   `blocking_issues`, `checks`, `reviewer{model,harness,mode}`, `source_refs`,
   `created_at`). Cross-reference `install-pack-semantic-gate.design/approach.md` §
   artifact as the source of truth. (DK9)
3. **The R1–R9 coverage bar** — enumerate every rule in `sys-skill-authoring-rules`;
   require a `checks` entry per rule (`pass|na|fail`) with a defended one-line
   justification; `na` must be justified, never silent; any `fail` is incompatible
   with `approve`. Do not hardcode the count — "every rule currently in
   `sys-skill-authoring-rules`." (DK10, DK11)
4. **The hash one-liner** — the exact `printf 'sha256:%s\n' "$(sha256sum ... )"`
   command, with the explicit instruction: run it *after* the skill YAML is final
   and `validate-pack` passed; hash the `pack.yaml` `skills[].file`, not a guess.
   (DK12)
5. **`reviewer` honesty** — how to fill `model`/`harness`/`mode`; the explicit
   warning that claiming `independent` in the authoring context is dishonest. (DK13)
6. **Header block** — `skill_id: sys-skill-review-verdict`, `category: tooling`,
   `always_apply: false`, `category_scope: tooling`, `author`, dated
   `change_summary`. Match the sibling meta skills exactly so `skill_md/parser.py`
   accepts it (verify against `parser.py`'s required fields at build orientation).

## T2 — Wire `sdd-add-skill.yaml` step 3 to emit the verdict  *(AC 7; DK9)*

Edit `src/agentalloy/_packs/sdd/sdd-add-skill.yaml` `raw_prose` step 3
("Self-critique against R1–R9"): keep the per-rule verdict discipline, add "**and
emit `review.yaml`** in the pack dir per `sys-skill-review-verdict`" so the critique
produces the machine-checkable artifact instead of prose only. Step 4/5 note that
when `AGENTALLOY_INSTALL_REQUIRE_REVIEW` is on, install requires the verdict (and
that `--allow-unreviewed` is the loud bypass). **Bump the sdd pack version**
(`pack.yaml`) so the edit propagates — pack edits propagate only on version bump.
Do not add new `prose_invariants` unless the hash one-liner proves load-bearing.

## T3 — AC-7 contract-fidelity test + fixtures  *(AC 7; DK14)*

`tests/install/test_review_producer_fidelity.py`:

- Commit a minimal fixture skill draft (any lint-clean skill YAML) + a golden
  `review.yaml` authored to T1's prescription (all R1–R9 keys, `approve`, correct
  hash computed over the fixture bytes).
- Assert `validate_review_verdicts(pack_dir, entries)` from the **real** slice-1
  module returns `.ok` (format fidelity).
- Assert the golden verdict's `checks` keys == the full R1–R9 id set parsed from
  `sys-skill-authoring-rules.md` (coverage fidelity — the anti-rubber-stamp guard).
- Assert a **partial** checks map (drop one rule) still validates at the gate but
  **fails** the coverage assertion — documents that coverage is the producer's job,
  not the gate's (DK4/DK10).
- No network, no LLM (mirrors slice-1 guard tests).

## Out of this slice (do not do here)

- Rebuilding / re-embedding the shipped corpus, or bumping the wheel/image version
  — a ship step (see design.md "honest scope boundary").
- Flipping `AGENTALLOY_INSTALL_REQUIRE_REVIEW`.
- Web-lane verdict surfacing + class-scoped independence — **slice 3**.
- Any edit to `pack_validation.py` or the install/validate wiring — **slice 1,
  frozen**.
