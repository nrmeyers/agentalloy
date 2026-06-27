# Plan #15 — Fragment Atomicity (§8): audit, reslice the tail, standing CI lint

**Source of truth:** `PLAN-OF-ATTACK.md` §8 + the §C doc-cap coupling.
**Owner decisions honored:** single-topic budget is ONE number shared by the
authoring lint / §C doc-cap / reslice target; the audit + reslice run **BEFORE**
the #13 K=2→4 test (a mis-sliced corpus makes the K sweep measure noise).

This item splits into **two deliverables with different batch classes**:

| Deliverable | Batch | What |
|---|---|---|
| **15-A Lint** | **CODE** (no re-embed) | token-budget soft warning in the authoring path + a standing CI gate that fails NEW oversized fragments |
| **15-B Reslice** | **CORPUS** (SkillVersion bump + re-embed + image rebuild) | split/trim the offender tail; batch the re-embed with §E/§F (#16/#17) |

## Locked decisions (per PLAN-OF-ATTACK §9 — these override any divergent value below)

- **D1 / single-topic authoring budget = 400 tok. LOCKED** (audit may nudge to ~450 only if the p90–p99 band is genuinely coherent). 15-A lint ceiling and 15-B reslice target both key off 400. ✓ matches this plan.
- **D4 / the §C Stage-B doc-cap = 2400 chars (~600 tok), NOT 512.** This plan's "§C doc-cap = 512 tok" is superseded — #09 ships a *char-based* 2400-char cap (~600 tok) per locked D4, sitting above the 400-tok authoring budget as a never-trigger floor once 15-B lands. The `rerank.py:_MAX_TOKENS = 512` constant is a separate downstream token-truncation guard; don't conflate it with #09's cap. Align §1's "512" reference to 600 tok / 2400 chars.

15-A ships immediately (catches regressions). 15-B rides the one shared corpus
rebuild with §F/§G. The CI gate is born with a **shrinking allowlist** so it is
green on arrival and goes fully-hard the moment the reslice empties the list.

---

## 0. The audit (RUN — read-only, reproduces §C numbers)

Script: token-estimate every **domain** fragment `content` with the codebase's
own heuristic `len(text) // 4` (`query_bounds._CHARS_PER_TOKEN = 4`;
`compose.py:227`, `vector_store.py:128`). 3,313 domain fragments across 36 packs.

```
content tok  -> p50=149  p90=306  p95=347  p99=441  max=1301  mean=172
docframe tok -> p50=156  p90=313  p95=355  p99=446  max=1309   (skill_id-prefixed, domain.py:786)

> 300 tok : 361 (10.9%)      > 450 tok : 26 (0.8%)
> 350 tok : 153 (4.6%)       > 500 tok : 15 (0.5%)
> 400 tok :  45 (1.4%)       > 600 tok :  6 (0.2%)
```

**Confirmed: a tail problem, not pervasive.** p90 is a healthy 306 tok; only the
top ~1.4% (45 fragments) clear 400 tok. Scope the reslice to that tail.

### Per-pack offender density (packs with ≥1 fragment >400 tok)

```
temporal        4/39    redshift   4/53    react      4/179   ui-design  3/151
snowflake       3/103   redis      3/116   python     3/179   fastify    3/97  (max 1301!)
fastapi         3/187   nestjs     2/106   linting    2/167   github-act 2/115
csharp-dotnet   2/56    typescript 1/201   nodejs     1/154   nextjs     1/180
java            1/136   design-rev 1/51    code-rev   1/20    analytics  1/57
```

### OFFENDER LIST (all 45 fragments > 400 tok content), classified

`MULTI` = genuinely multi-topic, **must split** into atomic fragments.
`DUMP` = long-but-coherent scrape/reference blob (table, anchor index, config) —
**trim or single-split**, low retrieval value as-is.
`COHER` = long-but-coherent prose, only ~30–90 tok over budget — split at the one
natural seam **or** accept if a clean seam doesn't exist (judgment, not forced).

| tok | class | skill_id · seq (type) | note |
|----:|-------|----------------------|------|
| 1301 | **DUMP** | fastify-error-handling-deep · 11 (execution) | scraped `## Fastify Error Codes` anchor index (~80 links, 0 prose) — **near-duplicate** of the row below |
| 1291 | **DUMP** | fastify-error-handling · 3 (execution) | identical anchor index — **dedup these two** then trim to a short pointer |
| 917 | **DUMP** | ui-design-tailwind-theme-and-tokens · 8 (example) | raw `<table>` namespace→utility map → compact markdown table |
| 688 | **DUMP** | ui-design-states-and-variants · 7 (execution) | large JSON config blob |
| 646 | **MULTI** | fastify-hooks-and-lifecycle · 11 (execution) | several lifecycle hooks (onResponse/Lifecycle…) → one fragment per hook group |
| 602 | **DUMP** | redis-pubsub-and-clients · 9 (setup) | client-library compatibility table |
| 574 | **COHER** | redshift-table-design · 2 (execution) | distribution-style prose; one seam |
| 562 | **COHER** | snowflake-time-travel-and-streams · 8 (rationale) | offset-storage prose |
| 543 | **MULTI** | snowflake-tables-and-clustering · 5 (verification) | 2× `###`: temp-table maintenance + naming conflicts → split 2 |
| 537 | **COHER** | redis-cluster · 6 (execution) | consistency prose |
| 533 | **DUMP** | linting-ruff-python · 3 (execution) | TOML config example |
| 530 | **MULTI** | temporal-activity-basics · 7 (example) | activity-execution subtopics |
| 524 | **DUMP** | linting-ruff-python · 4 (execution) | TOML config example |
| 508 | **MULTI** | design-review-rfc-and-adrs · 3 (rationale) | **13 headings**: ADR concept + when + template + full worked ADR → split 3–4 |
| 502 | **COHER** | redshift-query-tuning · 15 (rationale) | |
| 492 | **COHER** | nodejs-cluster · 4 (rationale) | |
| 489 | **MULTI** | github-actions-caching-and-artifacts · 3 (rationale) | caching + artifacts are two surfaces |
| 488 | **COHER** | react-transitions-and-deferred-values · 5 (verification) | |
| 482 | **COHER** | react-transitions-and-deferred-values · 3 (verification) | |
| 480 | **COHER** | ui-design-tailwind-utility-classes · 3 (execution) | |
| 476 | **DUMP** | snowflake-tasks-and-automation · 5 (example) | |
| 472 | **COHER** | java-records-sealed-and-patterns · 7 (guardrail) | |
| 466 | **COHER** | redshift-spectrum-and-iceberg · 10 (verification) | |
| 465 | **COHER** | github-actions-release-and-publish · 5 (execution) | |
| 460 | **MULTI** | temporal-activity-basics · 11 (execution) | |
| 452 | **MULTI** | temporal-schedules-and-timers · 3 (example) | schedules + timers |
| 449 | **COHER** | fastapi-middleware-patterns · 8 (execution) | |
| 449 | **COHER** | redshift-spectrum-and-iceberg · 11 (example) | |
| 448 | **COHER** | analytics-cohorts-and-sessions · 10 (example) | |
| 444 | **COHER** | python-async-patterns · 3 (execution) | |
| 444 | **COHER** | python-async-patterns · 4 (execution) | |
| 444 | **MULTI** | react-rendering-keys-and-memoization · 1 (rationale) | keys + memoization are two surfaces |
| 443 | **COHER** | redis-lua-functions · 5 (rationale) | |
| 433 | **MULTI** | nextjs-app-router-routing · 5 (rationale) | |
| 427 | **MULTI** | fastapi-dependency-injection · 1 (rationale) | |
| 427 | **DUMP** | nestjs-exception-filters · 3 (example) | |
| 418 | **COHER** | python-async-patterns · 2 (setup) | |
| 416 | **COHER** | nestjs-pipes-guards-interceptors · 8 (execution) | |
| 413 | **MULTI** | temporal-workflow-basics · 4 (execution) | |
| 411 | **COHER** | typescript-narrowing-and-control-flow · 5 (example) | |
| 407 | **MULTI** | code-review-debugging-and-defects · 4 (execution) | 5 headings / 3 fences — several debug techniques |
| 406 | **COHER** | fastapi-middleware-patterns · 4 (execution) | |
| 403 | **COHER** | react-tanstack-query · 5 (execution) | |
| 403 | **MULTI** | csharp-mstest-testing · 6 (example) | |
| 403 | **COHER** | csharp-async-and-tasks · 4 (setup) | |

**Tally:** 4 DUMP-pairs/blobs · ~13 MULTI · ~28 COHER (mostly 400–490, within
~25% of budget). **Reslice priority:** all DUMP + all MULTI (~21 fragments, the
real distortion); COHER 400–490 split only where a clean seam exists, otherwise
accept (do not force-split coherent prose 30 tok over budget).

**Audit reproduction:** `~/.local/share/uv/tools/agentalloy/bin/python` over
`src/agentalloy/_packs/**/*.yaml` (skip `pack.yaml`, `skill_class=="domain"`
only). The audit + classifier scripts are kept in scratch; fold a frozen copy
into `eval/audit_fragment_sizes.py` so the reslice can re-run it to confirm the
tail is gone (see 15-B step 4).

---

## 1. THE BUDGET DECISION (blocks everything — must be set first)

**Recommendation: single-topic budget = `400` tok (hard authoring ceiling).**

Rationale: 2× p90 (306) and just above p95 (347) — keeps the body of the corpus
untouched, targets exactly the 45-fragment tail; below 400 would force-split ~108
coherent 350–400 fragments for no retrieval gain. Pair with a **350-tok SOFT
warning** so new authoring drifts get flagged before they harden.

**Reconciliation with §C doc-cap (sibling item, the Stage B latency plan).** §C
proposed a runtime doc-cap of 550–600 tok to avoid head-truncating coherent
fragments. Once 15-B lands, **no fragment exceeds 400 tok**, so the §C doc-cap
should key off this budget, not the other way around:

- Authoring budget (this item): **400 tok** — what a well-sliced fragment is.
- §C runtime doc-cap: **§9 D4 LOCKS this at 2400 chars (~600 tok)** (char-based, in
  `lm_assist`), a margin above 400 that covers the `skill_id: ` doc-framing overhead
  (+~8 tok, `domain.py:786`) and **never triggers on a clean corpus**. On a clean
  corpus the doc-cap is a pure safety floor, exactly as §8 intends. *(Earlier drafts
  proposed 512 tok off `rerank.py:_MAX_TOKENS`; that is a separate downstream
  token-truncation guard, not #09's cap — superseded by D4.)*

So: **one authoring number (400)**; the runtime cap (D4: 600 tok) sits a notch above
it. Flag this locked value to the §C implementer as the binding input.

---

## 2. Deliverable 15-A — the standing lint (CODE batch, ship now)

All edits in `src/agentalloy/ingest.py`. The token estimator is inline
`len(content) // 4`, consistent with the rest of the codebase.

### 2.1 New constants (after line 74, beside the word thresholds)

```python
# Token-based single-topic atomicity budget (§8 fragment atomicity).
# Estimator: len(content) // 4, matching query_bounds._CHARS_PER_TOKEN.
# 400 = ~2x p90 / just above p95 of the live corpus; targets the ~1.4% tail.
_FRAG_TOKENS_BUDGET = 400   # hard ceiling (enforced in _validate once 15-B lands)
_FRAG_TOKENS_WARN  = 350    # soft warning (authoring drift signal)
```

> Note: the existing word ceilings (`_FRAG_WORDS_WARN_MAX = 800`,
> `_FRAG_WORDS_HARD_MAX = 2000`) are token-blind — 800 words ≈ 1330 tok, which is
> why the 1301-tok max never tripped. Leave them in place (they guard a different
> failure mode); the token budget is the binding atomicity gate.

### 2.2 Soft warning in `_lint` (insert in the per-fragment loop, after line 926)

```python
        est_tok = len(frag.content) // 4
        if est_tok > _FRAG_TOKENS_WARN:
            warnings.append(
                f"fragment sequence {frag.sequence} is ~{est_tok} tok; above the "
                f"{_FRAG_TOKENS_WARN}-tok single-topic budget (hard ceiling "
                f"{_FRAG_TOKENS_BUDGET}) — split at a topic boundary or trim a "
                f"scraped table/anchor-list (§8 fragment atomicity)"
            )
```

Non-blocking unless `--strict`; surfaces in the ingest summary
(`ingest.py:264-266`, `:382-405`).

### 2.3 Hard ceiling in `_validate` — **deferred-flip, gated on 15-B**

`_validate` (line 641) returns hard errors consumed by `install-packs` ingest;
adding a `> _FRAG_TOKENS_BUDGET` error there **now** would block install of the
bundled corpus until the reslice lands. So:

- **Step 1 (15-A, now):** do NOT add the hard error to `_validate` yet. The CI
  gate (2.4) with its shrinking allowlist is the enforcement surface in the
  interim.
- **Step 2 (with 15-B, allowlist empty):** add, in the domain branch after the
  word-ceiling block (after line 736):

```python
                est_tok = len(frag.content) // 4
                if est_tok > _FRAG_TOKENS_BUDGET:
                    errors.append(
                        f"fragment sequence {frag.sequence} is ~{est_tok} tok; "
                        f"single-topic budget is {_FRAG_TOKENS_BUDGET} — split or trim"
                    )
```

### 2.4 New CI gate — `tests/test_fragment_atomicity.py` (ship in 15-A)

Models `tests/test_bundled_corpus_integrity.py::_load_skill_docs`. The gate is
born GREEN via a frozen allowlist of the current 45 offenders and fails on any
**new** offender; a second test asserts the allowlist only ever shrinks.

```python
_BUDGET = 400  # keep == ingest._FRAG_TOKENS_BUDGET (asserted below)

# (skill_id, sequence) pairs currently over budget — REMOVE entries as 15-B
# reslices them; this set MUST only shrink. When empty, delete it AND flip
# ingest._validate to hard-fail (2.3 step 2).
_GRANDFATHERED: set[tuple[str, int]] = { ("fastify-error-handling-deep", 11), ... }  # all 45

def test_no_new_oversized_fragments() -> None:
    offenders = [(sid, seq, tok) for ... if tok > _BUDGET]
    new = [o for o in offenders if (o[0], o[1]) not in _GRANDFATHERED]
    assert not new, "new oversized fragment(s): " + ...

def test_grandfather_only_shrinks() -> None:
    # every allowlisted pair must still exist AND still be over budget;
    # a resliced (now-small) entry left in the list fails here -> forces cleanup.
    stale = [p for p in _GRANDFATHERED if p not in {(sid,seq) for ...over-budget...}]
    assert not stale, "remove resliced entries from _GRANDFATHERED: " + ...

def test_budget_constant_in_sync() -> None:
    from agentalloy.ingest import _FRAG_TOKENS_BUDGET
    assert _BUDGET == _FRAG_TOKENS_BUDGET
```

The full 45-pair `_GRANDFATHERED` literal is the OFFENDER LIST table above
(skill_id + seq columns). `test_budget_constant_in_sync` is the
config-consistency guard demanded by cross-cutting risk #7.

---

## 3. Deliverable 15-B — reslice the offender tail (CORPUS batch)

**Batch with §F (#17) / §G (#16) into ONE re-embed + image rebuild** (cross-cut
risk #3/#8: re-embed locks the running service's DuckDB; coordinate a restart).

### 3.1 Reslice mechanics (per offending fragment)

Each pack YAML carries both `raw_prose` (canonical body) and `fragments[]` whose
`content` MUST be a contiguous slice of `raw_prose` (enforced by `_lint`,
`ingest.py:883`). So reslicing = re-partitioning existing `fragments[]`:

1. **MULTI (~13):** split the fragment at its topic seam into 2–N new fragments,
   each ≤400 tok, each a still-contiguous `raw_prose` slice. Renumber `sequence`
   contiguously for the whole skill (the `_validate` contiguity check, line
   712-715, requires it) and set each new fragment's `fragment_type` to the
   dominant type of its half.
2. **DUMP (~8):** trim the scraped blob. The two fastify error-code anchor
   indexes (1301/1291 tok) are **near-duplicate, near-zero-value** navigation
   lists — collapse to a one-line pointer ("see Fastify docs `errors.md` for the
   full `FST_ERR_*` list") and **dedup** across the two skills. Convert the
   ui-design `<table>` (917) / redis client table (602) to compact markdown.
3. **COHER 400–490 (~28):** split only where a clean single seam exists; if the
   prose is genuinely one idea, leave it (a 30-tok overage is not a topic
   violation). Each one left over-budget stays in `_GRANDFATHERED` with a
   `# coherent, accepted` comment OR the budget is the wrong number — but per
   §1 the 400 line already excludes most of these by being generous.

### 3.2 Required-by-design corpus chores per touched skill

- **SkillVersion / version bump** on every edited pack (memory: pack edits
  propagate only on a version bump; `test_pack_version_bump_guard.py` enforces in
  CI; `pack_validation.check_version_gate` hard-errors changed-content/same-
  version). Bump the `version` field in each touched `pack.yaml`.
- **Re-embed** the new fragments (`agentalloy reembed` / `reembed/cli.py`):
  new/changed fragment_ids get fresh nomic vectors.
- **Image rebuild** (proxy is the container image, not the source tree).
- After reslice: empty `_GRANDFATHERED`, flip `_validate` to hard (2.3 step 2),
  and re-run `eval/audit_fragment_sizes.py` to assert `max ≤ 400` and the new
  p99 (should land ~360–390).

### 3.3 Do NOT regress retrieval

The contiguity invariant + version bump are the guardrails. Re-run the §E
retrieval regression and `eval/check_corpus_regression.py` baselines
(`name_probe_hit_rate=0.901`, `topic_probe_hit_rate=0.921`, `gold_hit=7/8`) after
re-embed; resliced fragments must not drop these (splitting should *help* the
multi-tag/BM25 dilution, never hurt name/topic probes).

---

## 4. Sequencing (the load-bearing constraint)

```
15-A lint (code)  ──ship now, independent──────────────┐
                                                        │
15-B reslice ──┐                                        │
§F corpus  ────┤  ONE re-embed + image rebuild ─────────┤
§G sdd prose ──┘                                        │
                                                        ▼
                              #13 K=2→4 retrieval test (sibling)
```

**#13 (the K test) MUST run after 15-B re-embeds.** Raising K and measuring
coverage against a corpus where a 1301-tok bundle can satisfy a query for the
wrong reason measures noise, not coverage. Clean the tail → re-embed once → then
sweep K. The §C runtime doc-cap stays regardless as a floor; on the cleaned
corpus it should never trigger.

---

## 5. Files touched / decisions / batching summary

**15-A (CODE, no re-embed):**
- `src/agentalloy/ingest.py` — constants (after :74), `_lint` soft warn (after
  :926), `_validate` hard ceiling (after :736, **flipped on only with 15-B**).
- `tests/test_fragment_atomicity.py` — NEW CI gate (allowlist + sync guard).
- `eval/audit_fragment_sizes.py` — NEW frozen audit script (re-run after reslice).

**15-B (CORPUS, re-embed + image rebuild):**
- ~20 pack YAMLs under `src/agentalloy/_packs/` (all DUMP + MULTI; selected
  COHER) + their `pack.yaml` version bumps. Heaviest: `fastify/` (dedup the two
  error-code dumps), `ui-design/`, `temporal/`, `design-review/`, `redis/`,
  `redshift/`, `snowflake/`, `linting/`.

**Sibling file overlap (conflict watch):**
- `src/agentalloy/_packs/**` — **§F (#17)** strips poison tags from
  `fastapi/`, `fastify/`, `temporal/`, `data-engineering/`; **§G (#16)** edits
  `sdd/`. 15-B touches `fastify/temporal/` too → **coordinate the same PR or
  rebase order** so version bumps and re-embed batch cleanly. §15 does NOT touch
  `sdd/`.
- `src/agentalloy/ingest.py` — no other item edits it (the §E/§F work is
  retrieval/pack content). Low conflict risk for 15-A.

**Decisions required before coding:**
1. **Budget = 400 tok** (recommended) — binds 15-A constants, 15-B reslice
   target, and the §C runtime doc-cap (→ 512). Sibling §C must consume this.
2. **DUMP policy:** confirm the two fastify `FST_ERR_*` anchor indexes are
   trimmed-to-pointer + dedup'd (recommended) vs kept-but-split.
3. **COHER 400–490 tail:** force-split or accept-in-allowlist? (recommended:
   accept where no clean seam; keeps the budget honest at 400).
4. **Re-embed batch membership:** 15-B + §F + §G in one rebuild (recommended).
