# Build 02 — Producer↔validator fidelity test

**Implements:** task T3 · **Satisfies:** AC 7 · **DKs:** DK14, DK10, DK11
**Design:** `install-pack-semantic-gate.slice2.design/{approach,test-plan}.md`

## Do

Create `tests/install/test_review_producer_fidelity.py`. Reuse the pack/skill
fixture builders already used by the slice-1 suites
(`tests/install/test_install_local_pack.py::_write_skill_yaml`,
`skill_file_sha256`). No LLM, no network.

1. **Golden accepted.** Author a `review.yaml` to `sys-skill-review-verdict`'s exact
   prescription (all R1–R9 keys, `verdict: approve`, `blocking_issues: []`,
   `reviewer.mode`, `target_hash` computed over the fixture skill bytes via
   `skill_file_sha256`). Assert the **real** `validate_review_verdicts(pack_dir,
   entries).ok` (import from `agentalloy.pack_validation`, do not reimplement).

2. **Coverage complete.** Parse the R-id set from
   `src/agentalloy/_packs/meta/sys-skill-authoring-rules.md` (regex on `## R<N>`),
   assert the golden `checks` keys == that full set. This is the anti-rubber-stamp
   guard and pins DK11 (R1–R9, driven by the doc, not a literal).

3. **Partial map: gate-ok but coverage-fail.** Drop one rule from the golden map;
   assert `validate_review_verdicts(...).ok` is still True (documents DK4 — the gate
   does not enforce coverage) **and** the coverage assertion from step 2 fails.

4. **Hash freshness regression.** Mutate the fixture skill by one byte; assert
   `validate_review_verdicts` now blocks (protects DK12/AC 2 from producer drift).

5. **No-LLM guard.** Assert the test module + the producer source import/reference
   nothing from `lm_client`/`authoring` and no LM endpoint (mirror
   `test_review_gate.py::test_gate_module_has_no_llm_or_network_imports`).

## Verify

- `uv run pytest tests/install/test_review_producer_fidelity.py -q` green.
- `uv run pytest -m "not integration" -q` green (run with `--extra code-index` for
  parity with CI — see memory `code-index-tests-error-without-extra`).
- `uv run pyright` 0 errors.

## Do NOT

- Assert against a live model producing the verdict (non-deterministic; out of
  scope — AC 7 is the fidelity contract).
- Edit `pack_validation.py` to make a test pass — if producer and validator
  disagree, the **producer** (build 01) moves.
