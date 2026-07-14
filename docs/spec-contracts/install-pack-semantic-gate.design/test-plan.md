# Install-Pack Semantic Gate — Slice 1 — Test Plan

> Runtime home: `docs/design/install-pack-semantic-gate/test-plan.md` (git-ignored).
> Committed copy. Maps each in-scope AC to concrete tests. Slice-1 ACs only:
> AC 1–6, CLI half of AC 8, AC 9–10. (AC 7 = slice 2; AC 8 web = slice 3.)

## Fixtures

- A minimal valid local pack dir (`pack.yaml` + one skill YAML) that already
  passes Gate 1, plus a `review.yaml` builder helper that produces an `approve`
  entry with a correct `sha256:` over the fixture skill's exact bytes.
- Mutators over the builder: drop the entry, corrupt `target_hash`, flip `verdict`
  to `revise`/`reject`, add a `blocking_issue`, empty `checks`, add a `fail` check,
  set `mode: self`.

## AC → tests

- **AC 1 — no verdict → blocked.** Install the fixture pack with `review.yaml`
  absent (or missing the skill's entry) → Gate 1.5 fails, exit 1, skill not
  ingested; `--allow-unreviewed` makes the same install succeed. *(Task 1 + 2.)*
- **AC 2 — stale verdict → blocked.** Build a valid verdict, then edit the skill
  YAML by one byte → `target_hash` mismatch → blocked. *(Task 1.)*
- **AC 3 — non-approving → blocked.** Parametrized over `verdict: revise|reject`,
  non-empty `blocking_issues`, empty `checks`, and a `fail` check → each blocks.
  *(Task 1.)*
- **AC 4 — valid approve → ingests.** Valid `approve` + matching hash + non-empty
  `checks` → Gate 1.5 passes and the skill ingests (end-to-end through
  `install_local_pack`). *(Task 2.)*
- **AC 5 — no LLM / no network in the gate.** (a) A no-network test (monkeypatch
  the HTTP transport to raise) drives Gate 1.5 to a pass and a fail — neither
  touches the network. (b) A **grep guard** asserts `pack_validation.py` and the
  Gate 1.5 code path never import `lm_client`, `authoring`, or an LM base URL.
  *(Task 1 + 3.)*
- **AC 6 — escape hatch recorded.** `--allow-unreviewed` bypasses and the result
  contract carries `gate_1_5.status == "bypassed"` with the reason (asserted on the
  contract dict, not just exit code). *(Task 2.)*
- **AC 8 (CLI half) — CLI backstop posture.** Default (process-forcing): a valid
  `mode: self` verdict passes on the CLI and the contract records `mode == "self"`
  and that no human review occurred. With
  `AGENTALLOY_INSTALL_REQUIRE_INDEPENDENT_REVIEW=1`: the same `mode: self` verdict
  is **rejected** and `mode: independent` passes. *(Task 2.)*
- **AC 9 — dry-run parity, zero side effects.** `validate-pack` on a pack with a
  failing verdict reports Gate 1.5 status and exits 1 with **no** ingest / reembed
  / network / corpus mutation (assert corpus row count unchanged + no store
  written). *(Task 3.)*
- **AC 10 — reuses Gate 1's report shape.** A pack with **both** a Gate-1 lint
  error and a Gate-1.5 verdict failure surfaces both in **one** aggregated exit-1
  report (assert both appear; assert exit code is the existing `1`, no new code).
  *(Task 1 + 2.)*

## Cross-cutting

- **Remote-pack parity (DK8).** The remote path applies the same gate: a fetched
  pack without `review.yaml` fails Gate 1.5; `--allow-unreviewed` overrides.
  Exercised with a stubbed manifest/fetch (no live network).
- **Mutation check.** New Gate 1.5 tests are mutation-tested against the validator
  (flip each predicate — hash-match, verdict-value, empty-issues, checks-present,
  no-`fail` — and confirm a test fails), per the repo's hermetic-e2e testing norm.
- **Determinism.** Gate 1.5 given the same pack + `review.yaml` yields the same
  outcome across runs (no clock/network dependence beyond `created_at` passthrough,
  which is recorded, not evaluated).
