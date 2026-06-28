# Spec — fixes from the big-calendar-ui SDD run feedback

Source: `~/dev/claude/aatest/feedback.md` (a full intake→ship dogfood of AgentAlloy on a
React/Vite calendar app). This spec covers only the AgentAlloy-side items (feedback bucket B,
plus A4 which overlaps B5). Bucket A1/A2/A3/A5 are the driving agent's own execution habits and
need no product change.

All findings below were verified against `main` (`b39a1fd`, v4.0.3), not taken from the
retrospective at face value. Where the feedback was stale or wrong, it is corrected here.

## Status correction vs. the feedback

| Item | Feedback said | Verified reality |
|------|---------------|------------------|
| B1 | gate is Python-only | **TRUE, unfixed** |
| B2 | qa cursor inherits build slug | **TRUE, unfixed** |
| B3 | exact-string heading match | **HALF-FIXED** (#271 added trailing-qualifier+case tolerance; leading-qualifier still fails) |
| B4 | qa not scaffolded | **TRUE, unfixed** — feedback's reviewer initially called this "already fixed (v3.11.1)"; a live `contract init --phase qa` run proves it still scaffolds nothing |
| B5/A4 | validator resolves related_contracts relative to contract dir | **TRUE but mischaracterized** — the shorthand used was never a supported convention; the real issue is CLI-vs-proxy validation asymmetry + a useless error |

Priority: **B1** (only real blocker) → **B4** (turns qa from "guess the contract" into a filled
template; obsoletes B3) → **B2**. **B3 dropped** (B4 makes it moot). **B5 deferred** (DX paper-cut,
revisit if it bites).

**Decisions (from the user):**
- B1 scope: Python + JS/TS detection only, plus an `extra_globs` arg for packs. No Go/Rust now.
- B3: dropped entirely — fixing B4 scaffolds the exact headings, so the agent never guesses.
- B5: deferred — not part of this batch.

---

## B1 — Stack-aware test gate (BLOCKER)

**Problem.** `build → qa` (and the `sdd-fast` lane) require Python test files to exist; a JS/TS
repo with 16 green Vitest tests fails the gate and must `--force` past a satisfied condition.

**Evidence.**
- `_packs/sdd/sdd-build.yaml:24-26` → `artifact_exists: { path: tests/**/*.py }`
- `_packs/sdd/sdd-fast.yaml:40` → same glob
- `signals/predicates.py:88-93` `eval_artifact_exists` is a binary glob existence check (no
  "counter"; the "0 tests" the feedback saw is just the empty-glob banner).
- Skill composition *is* stack-aware (domain_tags pulled React/Vite/calendar skills correctly),
  but `exit_gates` are product-owned: `signals/skill_loader.py` re-sources them from the shipped
  pack and a profile override may contribute only prose. So gates are **not** per-repo configurable
  by design — the fix belongs in the predicate layer, not in config.
- No existing project-wide toolchain detection (`eval_file_type_active:449` only inspects recent
  file events, not the repo).

**Proposed change (recommended, scope decided).** Add a `tests_present` predicate and use it in the
build/fast gates instead of the hardcoded Python glob. **Scope: Python + JS/TS + `extra_globs` only —
no Go/Rust this round.**

- New `eval_tests_present(args, ctx)` in `predicates.py`, registered in `PREDICATES` (line 566).
  MET if any recognized test file exists:
  - always: `tests/**/*.py`, `**/test_*.py`, `**/*_test.py`
  - if `package.json` at root: `**/*.{test,spec}.{ts,tsx,js,jsx,mts,cts}`
  - `args.extra_globs: [...]` — a pack can add a stack (incl. Go/Rust later) with no code change.
  - EXCLUDE `node_modules/**`, `dist/**`, `.venv/**` so vendored test files don't satisfy the gate.
- Swap `sdd-build.yaml` and `sdd-fast.yaml` `artifact_exists: tests/**/*.py` → `tests_present: {}`.
- Keep `artifact_exists: { path: src/** }` as-is.

**Alternatives considered.** (a) Multi-glob `artifact_exists` (any-of) — simpler but still
enumerated per pack and no `package.json` gating. (b) Per-repo gate config — rejected: violates the
shipped-first/product-owned invariant guarded by `test_config_consistency.py` / `test_skill_loader.py`.

**Risk.** Low. Predicate is additive; only two gate YAMLs change. Watch: don't let a stray
`node_modules/**/*.test.js` satisfy the gate — exclude `node_modules`, `dist`, `.venv`.

**Tests.** Unit-test `eval_tests_present` across py/ts/go fixtures incl. the node_modules exclusion;
update any gate snapshot tests; add a hermetic e2e that a Vitest-only repo passes build→qa without
`--force`.

---

## B4 — contract-init scaffolding skips qa & spec (CONFIRMED via live run)

**Problem.** `agentalloy contract init --phase qa --slug X` creates the contract but scaffolds **no**
`docs/qa/X.md`, so the agent authors the QA doc blind against an exact-heading gate. Design scaffolds
fine. Spec has the same latent bug.

**Live proof (main, this session).**
```
contract init --phase qa     --slug demo-feature  → "Created"  (no "Scaffolded docs" section)
contract init --phase design --slug demo-feature  → scaffolds approach.md / tasks.md / test-plan.md
find docs  →  only docs/design/demo-feature/*.md ; no docs/qa/ at all
```

**Root cause.** `subcommands/contract.py:_concretize_glob:214-226` substitutes only a literal
`<slug>` and a `**` path segment, then **returns `None` if any `*` remains**. Gate globs:
- design → `docs/design/**/approach.md` → `**`→slug → `docs/design/<slug>/approach.md` ✅
- qa → `docs/qa/*.md` → bare `*` survives → `None` → skipped ❌
- spec → `docs/spec/*.md` → same ❌

**Proposed change.** In `_concretize_glob`, after the `<slug>`/`**` substitutions, if the result is
exactly `dir/.../*.<ext>` (a single trailing basename wildcard, no other `*`), rewrite the basename
`*` to `<slug>`: `docs/qa/*.md` → `docs/qa/<slug>.md`. Keep returning `None` for any glob with a
non-terminal `*` or multiple wildcards (e.g. `.agentalloy/contracts/build/*.md`) so multi-file
globs are never scaffolded to one file. Confirm the build-contracts glob is `artifact_exists`
(not `artifact_contains`) so it is outside the scaffolding loop regardless.

**Risk.** Low/contained. Scaffolding is soft (wrapped in try/except, never overwrites). After the fix,
`docs/qa/<slug>.md` and `docs/spec/<slug>.md` get `## Checks`/`## Review` and
`## Acceptance Criteria`/`## Out of Scope` respectively.

**Tests.** Parametrize `_concretize_glob` over `docs/qa/*.md`, `docs/spec/*.md`,
`docs/design/**/approach.md`, `.agentalloy/contracts/build/*.md` (must stay `None`). Add a CLI test
asserting `contract init --phase qa` writes `docs/qa/<slug>.md` with both headings.

---

## B2 — work-item cursor inherits the terminal build slug

**Problem.** Entering qa, `.agentalloy/cursor` still points at the last build task
(`bcal-05-date-tests`), so qa expectations key off that slug instead of the feature slug.

**Evidence.** `api/proxy_signal.py:_resolve_current_contract:151-186` reads the cursor and only
falls back to "single contract in `contracts/<phase>/`" when the cursor fails to resolve — it never
resets the cursor on a phase change. The stale value persists across the transition.

**Proposed change.** On phase transition (where the phase file is written / `phase set` lands),
clear the cursor, or re-point it to the sole contract under `contracts/<newphase>/` when exactly one
exists (the spec/design/qa/ship single-item case). Fan-out phases (build, ≥2 contracts) stay
cursor-less until `task next`, matching current docstring semantics. Locate the phase-write seam
(`signals/skill_loader._read_phase` writers / `phase set` handler) and reset there.

**Risk.** Medium — touches phase-transition state shared per-repo (see the per-repo phase-contention
memory). Must not clobber a deliberately-set cursor mid-phase; only reset *on transition*.

**Tests.** e2e: build (cursor=build slug) → set qa → assert cursor is empty or = the qa contract,
and the qa gate keys off the feature slug. Guard the build fan-out case stays cursor-less.

---

## B3 — heading match discoverability (DROPPED — obsoleted by B4)

> **Decision: dropped.** Once B4 scaffolds `docs/qa/<slug>.md` and `docs/spec/<slug>.md` with the
> exact `## Checks` / `## Review` (and spec) headings, the agent no longer guesses heading names, so
> the matcher needs no change. Retained below as rationale only — no work item.

**Problem.** qa gate wants `## Checks` / `## Review`; the run authored `## Checks run` /
`## Code review` and got only a "1/2 sections" hint.

**Evidence.** `predicates._section_present:105-127` is case-insensitive and tolerant of a **trailing**
qualifier (#271): `## Checks run` → `## Checks` **matches today**. But `## Code review` does not start
with `review`, so leading qualifiers still fail. The "1/2" the run saw is consistent with only the
Review side failing — the feedback's "both failed" is stale.

**Why secondary.** Fixing B4 scaffolds the doc with the exact `## Checks` / `## Review` headings, so
the agent stops guessing. That removes most of B3's pain without touching the matcher.

**Options.**
- (a, preferred, low-risk) Surface the *required* header names in the gate/banner remediation text
  (`signals/gates.py`), so a miss says which headings are expected.
- (b) Loosen `_section_present` to also accept leading qualifiers. **Risk:** false-positive gate
  passes (a heading that merely contains the word). If pursued, require word-boundary + whole-token
  containment, and add tests that `## Codereview` does NOT satisfy `## Review`.

**Tests.** Lock current behavior (`## Checks run`✅, `## Code review`❌ vs `## Review`); if (b),
add the false-positive guards.

---

## B5/A4 — related_contracts resolution: CLI vs proxy asymmetry (DEFERRED)

> **Decision: deferred** — not part of this batch. `related_contracts` is only a soft BM25 boost, so
> compose output is unaffected; revisit if the CLI 400 bites again. Spec retained for when it does.

**Problem.** Manual `compose` 400'd on `related_contracts` while the in-run proxy tolerated the same
contracts.

**Evidence.**
- `contracts.py:166-183` resolves a relative entry first against the contract's own dir, then against
  the repo root (dir containing `.agentalloy`). The run wrote `intake/big-calendar-ui` — a
  **`contracts/`-relative, extension-less** shorthand that neither branch resolves. So this is a
  *format-never-supported* problem, not "resolves relative to the contract dir" as the feedback framed.
- CLI `compose` validates (`api/compose_router.py:112` → `validate_contract` →
  `contracts.py:312` `"Related contract not found: {rp}"`) → **400**.
- Proxy never validates: `api/proxy_apply.py:179-198` wraps `parse_contract` in try/except and on any
  failure logs a warning and passes through. Note `related_contracts` is a **soft BM25 boost** — broken
  paths do not degrade compose *output*; they only trip CLI validation.

**Proposed change.**
- Accept a third convention in `contracts.py` resolution: `contracts/`-relative (resolve against the
  nearest `.agentalloy/contracts/`), and auto-append `.md` when the entry has no suffix.
- Replace the bare error with one listing the conventions tried, e.g.
  `Related contract not found: 'intake/x' — tried <contract-dir>/intake/x.md, <root>/intake/x.md, <contracts>/intake/x.md`.
- Decide (open question): should the proxy *warn-surface* an unresolved related contract rather than
  swallow it? Leaning yes (a one-line telemetry/log note), keeping pass-through behavior.

**Risk.** Low. Resolution only widens (more forms resolve); error text only improves.

**Tests.** Parametrize resolution over all three conventions ± `.md`; assert CLI and proxy agree on a
contract whose related entries use the `contracts/`-relative shorthand.

---

## Resolved decisions
1. **B1** — Python + JS/TS detection now, with an `extra_globs` arg for future stacks (Go/Rust via
   config, not code).
2. **B3** — dropped; obsoleted by B4's scaffolding.
3. **B5** — deferred; CLI/proxy resolution unification not in this batch.

## This batch (today list) — SHIPPED (uncommitted working tree)
- **B1** — `tests_present` predicate (`predicates.py`) + registered + build/fast gates swapped
  off `tests/**/*.py`. Scope: pytest + JS/TS (package.json-gated) + `extra_globs`; node_modules/
  dist/.venv excluded. Unit tests in `test_predicates.py`.
- **B4** — `_concretize_glob` now maps a terminal basename wildcard to `<slug>.<ext>`
  (`docs/qa/*.md` → `docs/qa/<slug>.md`); non-terminal wildcards still return None. Live-verified
  qa+spec now scaffold. Tests updated/added in `test_contract_init_scaffold.py`.
- **B2** — phase transition drops `.agentalloy/cursor` via `_clear_state`, wired into BOTH the
  proxy seam (`skill_loader._write_phase_atomic`) and the CLI seam (`phase.run_phase_set`); an
  idempotent same-phase rewrite keeps the cursor. Tests in `test_task_cursor.py`.
- Pack bump: `sdd/pack.yaml` 1.0.21 → 1.0.22 (required by the pack-version guard; gate edits take
  effect from YAML at runtime, no re-embed needed for behavior).

Verification: ruff + ruff-format clean; pyright 0 errors; full unit suite 3268 passed / 2 skipped
(container edge-case file excluded — fails on a held :47950 + podman, unrelated to these changes).
Not done here: service version bump / commit / release — left for an explicit release step.

---

# Round 2 — proxy telemetry + build-tag guidance

A second session's analysis of the same big-calendar-ui run, ranked P1–P5. Re-verified
against `main`; corrections folded in. P1/P4/P2 shipped this batch; P3/P5 held as notes.

| # | Claim | Verdict | Correction that changed the action |
|---|-------|---------|------------------------------------|
| P1 | Native path emits zero timing | **TRUE, mis-scoped** | Orchestrator already measures retrieval (`compose.py` `LatencyBreakdown`); the whole persistence path (CompositionTrace fields + DB columns) exists. It was just never threaded. Pure threading, not new instrumentation. `assembly_ms` is structurally 0 (skip). The OpenAI surface is *also* blind to retrieval decomposition — not the clean reference implied. |
| P4 | Ship savings undercounted | **TRUE, not ship-specific** | `tokens_flat_equivalent` (compose.py) summed only domain skills → any system-only (Tier-1) compose reports flat=0 → 0% savings. Ship is just the common system-only case. |
| P2 | Domain guidance evaporates in build | **TRUE symptom, wrong cause** | Tier-2 re-compose on cursor move already works. Root cause from the run data: `bcal-04-navigation` tagged `[react]` not `[calendar]` — under-tagging during design decomposition. A deterministic subset/intersection gate CANNOT catch this (`react` is a valid feature tag), so the fix is prose guidance, not a gate. |
| P3 | Cross-phase re-injection | **TRUE, minor cite error** | Runtime dedup is best-per-skill within one compose; `dedup_hard_threshold=0.92` is authoring-only (unused at runtime). Low value, plausibly intentional re-grounding. **Held as note.** |
| P5 | qa/build prose drives low savings | **Mostly FALSE** | design is the heaviest prose (7894 ch) yet had the *highest* savings — "prose weight → low savings" is backwards. Low spec/qa savings is mostly the P4 flat-undercount. **Held as note: re-measure after P4 before trimming any prose.** |

## P1 — thread compose latency (SHIPPED)
- `ProxyComposeTelemetry` gains `retrieval_latency_ms` / `total_latency_ms` (proxy_apply.py);
  `_merge_compose_telemetry` sums each leg's `latency_ms` (guarded — only `ComposedResult` carries
  it; `None` when untimed, distinct from 0). `write_proxy_trace` gains a `retrieval_latency_ms`
  param threaded to the existing `CompositionTrace` field. Both surfaces pass it; the passthrough's
  hardcoded `total_latency_ms=None` now carries compose-span total. `assembly_ms` left out (always 0).
- Tests: merge sums leg latency / None on passthrough / ignores EmptyResult; trace round-trip.

## P4 — count system skills in flat baseline (SHIPPED)
- compose.py flat loop now sums raw_prose over `source_skills ∪ system.applied_skill_ids`. Fixes
  every system-only compose (ship et al.), and also makes domain composes' flat baseline include the
  system prose they inject. Test: a `legs="system"` compose now reports flat>0.
- Re-measure run savings after this lands; P5 likely dissolves.

## P2 — build work-item tag guidance (SHIPPED, prose)
- sdd-design-and-planning.yaml §6 gains a `MUST: tag the most specific surface the task implements,
  not the generic framework` rule, citing the navigation-mistagged-`[react]` failure directly.
  Covered by the existing sdd pack bump (1.0.22). A hard gate is explicitly NOT added — it can't
  distinguish a valid-but-generic tag from the right one.
- Deferred (NOT this PR): Tier-2 fires once-per-cursor-move (not per turn); raising `TIER2_K`/rerank.

## Held as notes (not tasks)
- **P3** — session-level "recently-injected" fragment suppressor. A/B first; small token win.
- **P5** — workflow-prose trimming. Gate on a post-P4 re-measurement, not the current numbers.
