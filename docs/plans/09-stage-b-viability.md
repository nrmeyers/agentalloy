# TODO #9 — Stage B viability (§C latency + §D selection)

**Batch:** CODE (pure code/config; NO corpus re-embed, NO SkillVersion bump).
**PR grouping:** PR-3 `stage-b/viability` (findings #1 + #2 ship together — latency alone regresses quality).
**Effort:** L overall (doc-cap = S, slots+shared-bound = M, selection+threshold+health+breaker = M).
**Risk:** medium (changes the live HIT path; gated by `LM_ASSIST=arbitrate`, GPU presets only).

> Ship §C and §D together. Shipping §C alone makes Stage B HIT, and today's HIT path
> bypasses `skill_granular_select` and filters at an inert `keep_threshold=0.05` in fusion
> order → it turns off the measured-good diversity selection for no benefit (Cross-Cutting
> Risk #2). The latency one-liner may be cherry-picked as a hotfix **only if** `LM_ASSIST`
> stays gated until §D lands.

---

## Locked decisions (per PLAN-OF-ATTACK §9 — these override any divergent value below)

- **D1 / authoring budget = 400 tok** (single-topic). The §C runtime doc-cap is a *separate, higher* floor — see D4. Do not collapse the two into one number.
- **D4 / Stage B doc-cap = 2400 chars (~600 tok). LOCKED.** Closes the §C "1600 vs 2400" A/B in §1/D1 — ship 2400. `_DEFAULT_DOC_CAP_CHARS = 2400` is correct; the "if owner insists, drop to 1600" hedge is superseded. (#15's "512-tok" reference is the separate `rerank.py` token-truncation guard, not this char-based cap — don't conflate.)
- **D5 / reranker slots = `--parallel 8 -c 8192`. LOCKED default** — measure throughput-vs-slots on the Vulkan build before considering 12.
- **D6 / `keep_threshold` = MEASURE-THEN-SET (do NOT guess a prod value). LOCKED posture.** Ship the env knob; leave it **gated-off / inert (code default 0.0)**, and do **not** bake 0.30/0.40 into the GPU presets as a live value (see §4.2 — superseded). **(Live-test correction: 0.05 is NOT inert — the keep test is `score >= threshold` and the reranker scores genuinely-irrelevant candidates exactly 0.0, so 0.05 empties such a task. 0.0 keeps all, reorders only.)** The reranker's real P(yes) distribution over labelled fragments is a **hard decision gate** before any prod threshold; 0.30 and ~0.4 are A/B *starting points*, not ship values. Until that measurement, Stage B "works" via D4+D5 (it HITs instead of timing out) and §4.1's survivors→`skill_granular_select` plumbing (diversity restored) — the threshold filter stays inert.

---

## 0. Runtime ground-truth (verified live, this worktree, 2026-06-27)

Probed the live env so the engineer does not re-investigate:

- **Reranker slots:** `~/.local/share/agentalloy/logs/rerank-server.log` →
  `n_parallel is set to auto, using n_parallel = 4 and kv_unified = true`; `n_slots = 4`;
  every slot `n_ctx = 40960`. So the launcher sets **neither `--parallel` nor `-c`** and
  llama.cpp auto-picks `n_parallel=4`, `n_ctx=40960` (full Qwen3 train ctx). Confirms §C root cause.
- **Listeners (`ss -ltnp`):** `127.0.0.1:47950` = uvicorn (native), `127.0.0.1:47951` = llama embed,
  `127.0.0.1:47952` = llama rerank. **Only one listener on 47950** (native uvicorn) in this env —
  the container/native dual-listener drift (Risk #1 / #14) is **NOT present here right now**. Still
  coordinate with #14 before trusting any prod measurement, but localhost is unambiguously the
  native tool here.
- **`/health` lies:** `GET /health` returns `status:"healthy"`, `lm_assist.mode:"arbitrate"`,
  `keep_threshold:0.05`, `timeout_ms:1500` — while Stage B times out ~100%. Health never probes 47952.
  This is exactly the §D-C bug.
- **Implication for the doc-cap rationale:** because runtime `n_ctx=40960` (huge), oversized fragments
  are **not** silently truncated at the current `-c` (the PLAN's "truncate at current -c" framing is
  moot at this config). The doc-cap's real lever here is **prefill cost**: a 1,301-tok fragment costs
  ~1.3k prefill tokens per request × 12 reqs serialized over 4 slots = the budget killer. Cap → bounded
  prefill. (Once we add `-c 8192`, the anti-truncation framing becomes live too.) Keep the doc-cap as
  the primary fix; just describe it as a prefill/latency bound, not anti-truncation, at today's `-c`.

---

## 1. Decisions to make BEFORE coding

| # | Decision | Recommendation | Owner-doc anchor |
|---|----------|----------------|------------------|
| D1 | **Doc-cap value & unit.** | **Char-based** `LM_ASSIST_DOC_CAP_CHARS=2400` (~600 tok @ ~4 ch/tok). Char-based mirrors the classifier's `_MAX_INPUT_CHARS=2000` (classifier.py:114) and needs no tokenizer. Covers corpus p99=433 tok fully; only knifes the genuine >p99 tail (max 1,301). **§9 D4 LOCKS this at 2400 chars (~600 tok) — A/B closed, ship 2400** (the 400-tok figure is the *authoring* budget D1, kept distinct). | §C step 1 |
| D2 | **Doc-cap vs §8 single-topic budget reconciliation.** Owner note says ~350–400 tok is ONE number for authoring-lint + §C cap + reslice. | Keep them **distinct by design**: authoring budget ~400 tok is what a fragment *should* be; the §C runtime cap sits *above* it (~600 tok) as headroom so a properly-sliced fragment is *never* truncated and the cap "essentially never triggers" (§8's own words). The CODE batch ships now against the still-fat corpus, so 600 (not 400) avoids mis-scoring today's 1,301-tok outliers. After the §8 reslice (CORPUS batch, later) the cap is a pure floor. ~~If owner insists, drop to 1600~~ — **superseded by §9 D4: cap is locked at 2400 chars / ~600 tok** (the 400-tok number is the *authoring* budget D1, kept distinct). | §8 open question |
| D3 | **`keep_threshold` recalibration target.** 0.05 is near-inert; classifier uses 0.45 on the *same* model but a different (intent) instruct framing. | **§9 D6 OVERRIDES: measure-then-set, ship gated-off.** Ship the `LM_ASSIST_KEEP_THRESHOLD` env knob with the **inert code default (0.0; 0.05 is NOT inert — live test emptied the calendar task)**; do **NOT** bake 0.30 into the GPU presets (see §4.2 — superseded). 0.30/~0.4 are A/B starting points; the reranker's real P(yes) distribution is a **hard decision gate** before any prod value. Do not blindly copy 0.45 either. | §C/§D-B |
| D4 | **Selection fix: sort-by-score vs feed-into-`skill_granular_select`.** | **Feed survivors into `skill_granular_select`** (Option B). It keeps the measured-good depth+diversity selection instead of trusting a 0.6B model's absolute ordering, and uses Stage B as the keep/drop *filter* it was designed to be. Sorting-by-score (Option A) discards the depth guarantee. This **does change the documented behavior** (`docs/lm-assist-design.md:125,129` say "in fusion order") → update that doc in the same PR. | §D-A |
| D5 | **Server sizing.** | `--parallel 8 -c 8192` (1,024 tok/slot, 2 waves, ~⅕ the KV of the current 40960). **Do NOT assume linear Vulkan slot scaling** — measure throughput-vs-slots before choosing 8 vs 12. Lower `-c` also frees VRAM on the shared 3060 for the co-tenant embed server. | §C step 2 |
| D6 | **Shared client-pool bound across the two `FragmentScorer` singletons.** | One config knob `LM_ASSIST_MAX_CANDIDATES` (default 8, = `--parallel`) feeds BOTH `lm_assist._MAX_CANDIDATES` and the pool `max_workers`; both scorers cap at it so compose-StageB (≤8) + intent (≤1, single doc) can't re-oversubscribe. See §3.3. | §C step 3 |

---

## 2. Files touched (for cross-item conflict detection)

| File | Change | Shared with |
|------|--------|-------------|
| `src/agentalloy/retrieval/lm_assist.py` | doc-cap, pool-bound knob, per-req margin, scorer plumbing | — |
| `src/agentalloy/retrieval/domain.py` | `_maybe_lm_arbitrate` selection rewrite (filter→`skill_granular_select`) | **#13 (§E) — CONFLICT, see §6** |
| `src/agentalloy/retrieval/rerank.py` | `_FailureLatch` long-open escalation (shared latch class) | #13 only reads it indirectly |
| `src/agentalloy/signals/classifier.py` | adopt shared `LM_ASSIST_MAX_CANDIDATES` bound on the intent scorer | #14 (env/port drift) reads same module |
| `src/agentalloy/api/health_router.py` | add reranker probe → degraded when timeout-dominant | #14 (health/listener) likely overlaps — coordinate |
| `src/agentalloy/install/subcommands/start_rerank_server.py` | add `--parallel 8 -c 8192` to `cmd` (line 163-171) | #14 (rerank launcher / ports) — coordinate |
| `src/agentalloy/install/presets/{nvidia,radeon,apple-silicon}.yaml` | add `LM_ASSIST_KEEP_THRESHOLD`, `LM_ASSIST_MAX_CANDIDATES` | #13 if it edits presets |
| `.env.example` | document the 3 new knobs (after line 74) | — |
| `tests/test_config_consistency.py` | guard new preset knobs | #13 |
| `tests/test_lm_assist.py` | new selection/cap/pool tests; **update `test_arbitrate_threshold_filters`** | — |
| `tests/test_health.py` | reranker-probe degraded test | #14 |
| `tests/test_classifier_reranker.py` | shared-bound assertion (optional) | #14 |
| `docs/lm-assist-design.md` | update §"Stage B" to filter-into-`skill_granular_select` semantics | — |

---

## 3. §C — Latency

### 3.1 Doc cap (PRIMARY) — `retrieval/domain.py` `_maybe_lm_arbitrate`

The documents list is built at **domain.py:787**:
```python
documents = [f"{f.skill_id.replace('-', ' ')}: {f.content}" for f in head]
```
There is **no length bound** — the embed path bounds its *query* (build_retrieval_query, domain.py:422)
and the classifier bounds its *query* (`text[:_MAX_INPUT_CHARS]`, classifier.py:277), but the rerank
*documents* are uncapped. Add the symmetric guard.

**Where:** put the cap in `lm_assist.py` so BOTH consumers inherit it (the classifier passes a short
intent desc, so it's a no-op there) — but the fragment-body bloat is compose-specific, so cap at the
`build_prompt` boundary inside `FragmentScorer._score_one` (lm_assist.py:263-266):

before:
```python
def _score_one(self, task: str, document: str) -> float:
    payload: dict[str, Any] = {
        "model": self._config.model,
        "prompt": build_prompt(task, document, instruct=self._config.instruct),
```
after:
```python
def _score_one(self, task: str, document: str) -> float:
    payload: dict[str, Any] = {
        "model": self._config.model,
        "prompt": build_prompt(task, document[: self._config.doc_cap_chars], instruct=...),
```
Add `doc_cap_chars: int = _DEFAULT_DOC_CAP_CHARS` to `LMAssistConfig` (lm_assist.py:95-106), a module
const `_DEFAULT_DOC_CAP_CHARS = 2400` (lm_assist.py near :64), and resolve
`doc_cap_chars=_env_int("LM_ASSIST_DOC_CAP_CHARS", _DEFAULT_DOC_CAP_CHARS)` in `load_config`
(lm_assist.py:147-154). The classifier's `LMAssistConfig(...)` (classifier.py:207-216) inherits the
default — fine (its document is a short intent desc).

> Rationale at today's `n_ctx=40960`: this is a **prefill bound**, not anti-truncation (see §0).
> It bounds each request's prompt to ~700 tok regardless of fragment size and slashes the prefill that
> drives the timeout. It also mirrors the embed-ceiling guard philosophically and becomes a true
> anti-truncation guard once `-c` drops to 8192 (§3.2).

### 3.2 Explicit server slots — `install/subcommands/start_rerank_server.py:163-171`

The launch `cmd` list has no `--parallel`/`-c`:
```python
cmd = ["llama-server", "--port", str(LLAMA_RERANK_PORT), "-ngl", str(ngl), "-m", str(model_path)]
```
after:
```python
cmd = [
    "llama-server",
    "--port", str(LLAMA_RERANK_PORT),
    "-ngl", str(ngl),
    "-m", str(model_path),
    "--parallel", str(_RERANK_PARALLEL),   # new module const = 8
    "-c", str(_RERANK_CTX),                 # new module const = 8192
]
```
Add module consts near line 35: `_RERANK_PARALLEL = 8`, `_RERANK_CTX = 8192`. (1,024 tok/slot, 2
decode waves for 8 docs, frees ~⅘ of the current 40960 KV → eases 3060 co-tenancy with the 47951 embed
server.) **Idempotency caveat:** the launcher exits early if 47952 is already reachable (line 105-119),
so this only takes effect after the rerank server is restarted — call out in the PR that the GPU host
must restart the rerank server (or run `agentalloy` setup) for the new slots to apply. Verify post-change
by grepping the journal/log for `n_parallel = 8` (do NOT assume).

> D5: measure throughput-vs-slots on the Vulkan build before committing 8 vs 12 — staggered slot-launch
> gaps in the log suggest partial per-wave serialization, so more slots may not scale linearly.

### 3.3 Bound + share the client pool — one config knob across BOTH scorers

Today `FragmentScorer.__init__` (lm_assist.py:261) hardcodes
`ThreadPoolExecutor(max_workers=_MAX_CANDIDATES)` with `_MAX_CANDIDATES=12` (lm_assist.py:64). Two
independent singletons exist: compose Stage B (`build_scorer_from_env`, lm_assist.py:341) and intent
(`build_intent_scorer_from_env`, classifier.py:224) — each builds its own 12-wide pool → up to 24
concurrent on N slots.

Changes:
1. **`_MAX_CANDIDATES = 12 → 8`** (lm_assist.py:64), env-overridable:
   `_MAX_CANDIDATES = _env_int("LM_ASSIST_MAX_CANDIDATES", 8)` at module load (or resolve in
   `max_candidates()`). 8 = the `--parallel` slot count → compose Stage B fans exactly one wave-set.
2. **Pool width keyed to the same knob:** `ThreadPoolExecutor(max_workers=max_candidates(), ...)` in
   `__init__` (lm_assist.py:261) so pool width can never drift from the candidate cap.
3. **Shared bound for the intent scorer:** the classifier scorer scores a *single* document per call
   (classifier.py:277 `score(text, [desc])`), so its pool is effectively width-1 in practice; the real
   co-tenancy risk is the two singletons firing in the same request window. The cheap, correct guard is
   the **shared knob** (both read `max_candidates()`), so `--parallel 8` accommodates `8 (compose) +
   1 (intent) = 9` worst-case — set `--parallel 8` with the understanding compose dominates; if A/B
   shows contention, bump to `--parallel 9`. (A truly shared bounded queue across both singletons is the
   heavier alternative — defer unless A/B shows oversubscription; note it in §7 open items.)
4. **Per-request margin (cleanup, §D-D):** `__init__` sets `per_req_s = config.timeout_ms/1000.0`
   (lm_assist.py:255) — i.e. the httpx per-request timeout equals the whole batch deadline. Make it
   strictly under: `per_req_s = config.timeout_ms / 1000.0 * 0.9` so a single hung request can't consume
   the entire batch budget before the deadline loop reaps it. Low-risk, isolated.

---

## 4. §D — Selection correctness & observability

### 4.1 (A) Selection — filter, then `skill_granular_select` (stop bypassing it)

The HIT path today (domain.py:597-602) **replaces** diversity selection:
```python
lm_selected, lm_outcome, lm_detail = _maybe_lm_arbitrate(ranked, query, k)
if lm_selected is not None:
    selected = lm_selected                       # plain fusion-order top-k, diversity OFF
    skills_ranked = list(dict.fromkeys(f.skill_id for f in ranked))
else:
    selected, skills_ranked = skill_granular_select(ranked, k)
```
and inside `_maybe_lm_arbitrate` (domain.py:802-804) it keeps in **fusion order** and hard-caps at k:
```python
threshold = load_config().keep_threshold
kept = [frag for frag, score in zip(head, result.scores, strict=True) if score >= threshold]
selection = kept[:k]
```

**Rewrite `_maybe_lm_arbitrate` (Option B):** keep Stage B as a relevance *filter*, then run the
deterministic depth+diversity selection over the survivors. Change the return contract so it returns the
**filtered ranked survivors** (not a final selection) plus telemetry, and let the call site run
`skill_granular_select` over them.

New `_maybe_lm_arbitrate` tail (replacing domain.py:802-813):
```python
threshold = load_config().keep_threshold
survivors = [frag for frag, score in zip(head, result.scores, strict=True) if score >= threshold]
# Tail beyond the scored head is untouched by Stage B — keep it as lower-priority
# candidates so k can still be filled when the head is aggressively pruned.
survivors = survivors + ranked[max_candidates():]
scores = {f.fragment_id: s for f, s in zip(head, result.scores, strict=True)}
dropped_ids = [f.fragment_id for f in head if f.fragment_id not in {s.fragment_id for s in survivors}]
detail = _LMArbitrationDetail(kept_ids=[f.fragment_id for f in survivors], dropped_ids=dropped_ids, scores=scores)
return survivors, LMAssistOutcome.HIT, detail   # survivors, NOT a k-capped selection
```
Call site (domain.py:597-602) becomes:
```python
lm_survivors, lm_outcome, lm_detail = _maybe_lm_arbitrate(ranked, query, k)
if lm_survivors is not None:
    selected, skills_ranked = skill_granular_select(lm_survivors, k)   # diversity PRESERVED
else:
    selected, skills_ranked = skill_granular_select(ranked, k)
```
This (a) drops genuinely off-task fragments via Stage B, (b) keeps the depth/round-robin guarantee, and
(c) collapses the two branches to a single `skill_granular_select` call — so a HIT no longer means
"diversity off." **`kept_ids` in telemetry now means "survived the filter," and the final injected set is
the post-`skill_granular_select` result** — note the semantic shift in the PR + design doc.

> Empty-keep is still valid: if all head fragments are sub-threshold AND the tail is empty, `survivors`
> is `[]` and `skill_granular_select([], k)` returns `[]` — the "inject nothing" case is preserved.

> Alternative (Option A, sort-by-score) if owner overrides D4: replace `selection = kept[:k]` with
> `kept.sort(key=lambda f: scores[f.fragment_id], reverse=True); selection = kept[:k]` and keep the
> bypass. Simpler, but discards diversity — not recommended.

### 4.2 (B) `keep_threshold` — **§9 D6: measure-then-set, ship gated-off**

> **Reconciled with §9 D6.** Stage B's threshold ships as a *knob*, not a guessed prod value.
> The CODE batch ships the env wiring + the inert code default; the **P(yes) measurement is a hard
> decision gate** before the GPU presets carry any live threshold. Do NOT bake 0.30 into the presets
> in this batch.

- Code default `_DEFAULT_KEEP_THRESHOLD = 0.0` (lm_assist.py) → **this is the gated-off / fail-open
  default.** Stage B still HITs (D4+D5) and still feeds `skill_granular_select` (§4.1), so diversity
  selection is restored — the *filter* is inert (keeps every `score >= 0.0`) until measured. **0.0, not
  0.05:** the live test proved 0.05 is not inert (the reranker scores genuinely-irrelevant candidates
  exactly 0.0, which 0.05 would drop → empty result; 0.0 reorders without dropping).
- `LM_ASSIST_KEEP_THRESHOLD` is already env-wired (`load_config`, lm_assist.py:152). Ship the knob and
  document it in `.env.example`; **do not set it in the presets yet.**
- **Decision gate D6 (post-batch):** measure the reranker's P(yes) distribution over a labelled
  fragment-relevance set; set the prod threshold from that (~0.4 is a placeholder ballpark only). Only
  then add `LM_ASSIST_KEEP_THRESHOLD` to the GPU presets + the `test_config_consistency` guard. Do NOT
  copy 0.45 from the classifier blindly (different instruct framing).

### 4.3 (C) Observability — `/health` probes the reranker; breaker escalates

**Health probe** — `api/health_router.py`. Add a reranker probe alongside the existing three
(check() at health_router.py:76-132):
- New `_probe_reranker(self) -> str | None`: when `load_config().enabled`, do a **cheap real probe** —
  a 1-doc `FragmentScorer.score("health", ["ping"])` with a short timeout, OR query a rolling
  timeout-rate counter (see below). Return a detail string when the last N Stage B outcomes are
  timeout-dominant, else `None`. Wire into `asyncio.gather` (health_router.py:77-81) and add a
  `"reranker"` entry to `deps` (health_router.py:86-113) with
  `impact="Stage B fragment re-rank timing out; falling back to deterministic selection"`.
- **Promote to `degraded`** when the reranker probe is non-None: extend the `elif` at
  health_router.py:117 → `elif embed_ok is not None or tel_ok is not None or rerank_err is not None:`.
  (Stage B failing is degraded, never unavailable — it fails open.)
- Simplest signal source: a process-local rolling window of the last K `LMAssistOutcome`s recorded in
  `FragmentScorer.score` (increment a module counter on TIMEOUT/HIT). The health probe reads the ratio.
  This avoids a synchronous probe call adding latency to /health. **Recommend the counter approach.**

**Breaker long-open escalation** — `retrieval/rerank.py` `_FailureLatch` (rerank.py:52-92), the class
**shared** by Stage A, Stage B (lm_assist.py:44,252), and intent. Today: open after 3 failures, auto-reset
after a 60 s cooldown, allow one retry — so a permanently-dead reranker re-arms and times out forever
every 60 s. Add escalation:
- New field `_consecutive_opens = 0` and `_long_cooldown = _LONG_COOLDOWN_SECONDS` (e.g. 600 s).
- In `allow()` (rerank.py:67-76), when the cooldown elapses, **before** resetting, if the *next* attempt
  fails again increment `_consecutive_opens`; once `>= _ESCALATE_AFTER` (e.g. 3) cooldown-then-fail
  cycles, switch the effective cooldown to `_long_cooldown`. Reset `_consecutive_opens` on
  `record_success`.
- Keep it process-local, never persisted (matches the existing philosophy). Guard with a unit test that
  drives 3 open→retry→fail cycles and asserts the 4th stays open past 60 s.

> NOTE: `_FailureLatch` is shared by Stage A and the intent classifier too — escalation applies to all
> three consumers. That's desirable (a dead reranker should long-open everywhere), but call it out so the
> behavior change is intentional, not a surprise to #14.

### 4.4 (D) Cleanups

- `per_req_s` strictly under batch budget — done in §3.3(4).
- Fix the stale `__init__` comment (lm_assist.py:241-242 "so 12 docs fit the 300 ms budget" — both 12 and
  300 are now wrong) → "so up to `max_candidates()` docs fit the configured timeout."
- The `score()` deadline loop already bounds wall-clock (lm_assist.py:289-294) — no change.

---

## 5. Test plan (names)

`tests/test_lm_assist.py`:
- `test_score_one_truncates_document_to_doc_cap` — MockTransport captures the posted prompt; assert a
  3,000-char document is truncated to `doc_cap_chars` before `build_prompt`.
- `test_pool_width_equals_max_candidates` — `FragmentScorer(cfg)._pool._max_workers == max_candidates()`.
- `test_max_candidates_env_override` — `LM_ASSIST_MAX_CANDIDATES=6` → `max_candidates()==6` and pool width 6.
- `test_per_req_timeout_under_batch_budget` — assert the httpx client timeout < `timeout_ms/1000`.
- `test_arbitrate_filters_then_diversity` (NEW, replaces the bypass behavior) — stub HIT scores
  `[0.9,0.01,0.6]`, threshold 0.3 → survivors `[f1,f3]` fed to `skill_granular_select`; assert call site
  routes through `skill_granular_select` (monkeypatch/spy) and diversity ordering applies.
- **UPDATE `test_arbitrate_threshold_filters` (currently lm_assist.py test:268)** — it asserts the old
  fusion-order `["f1","f3"]` return *as the final selection*; under Option B `_maybe_lm_arbitrate` now
  returns *survivors* not a k-capped selection, so update the assertion to survivors + add a separate
  call-site test for final selection. (f1=0.9,f3=0.6 both survive 0.30; f2=0.01 drops.)
- `test_arbitrate_empty_keep` (existing, lm_assist.py test:288) — still `[]`; verify it holds under the
  survivors+tail rewrite (tail empty → `[]`).
- `test_arbitrate_survivors_include_unscored_tail` — `ranked` longer than `max_candidates()`; assert
  fragments past the head survive as lower-priority candidates.

`tests/test_rerank.py`:
- `test_latch_escalates_to_long_open` — drive `_ESCALATE_AFTER` open→cooldown→fail cycles; assert
  `allow()` stays False past the normal 60 s cooldown (monkeypatch `time.monotonic`).
- `test_latch_resets_escalation_on_success` — success clears `_consecutive_opens`.

`tests/test_health.py`:
- `test_reranker_timeout_dominant_reports_degraded` — seed the rolling counter timeout-dominant, assert
  `overall=="degraded"` and a `"reranker"` dependency appears with status `"unavailable"`.
- `test_reranker_healthy_does_not_degrade` — HIT-dominant counter → `"healthy"`, reranker dep ok.

`tests/test_config_consistency.py`:
- Extend `test_preset_lm_assist_posture` (test:107) — for `arbitrate` presets assert
  `LM_ASSIST_MAX_CANDIDATES` present and `== "8"` (drift guard vs the `--parallel` value).
  **Per §9 D6, the presets do NOT carry `LM_ASSIST_KEEP_THRESHOLD` in this batch** — assert it is
  *absent* (threshold stays gated-off at the inert code default until the P(yes) measurement). Add the
  preset assertion only after D6 sets the measured value.
- New `test_rerank_parallel_matches_max_candidates` — assert `start_rerank_server._RERANK_PARALLEL >=
  lm_assist.max_candidates()` (the slot count must accommodate the client fan-out).

Mutation checks (per Risk #7 / testing-strategy memory): restore `_MAX_CANDIDATES=12` and confirm the
pool-width test fails. (No keep_threshold preset assertion this batch — D6 is measure-then-set.)

---

## 6. Cross-item conflict — **#13 shares `retrieval/domain.py`**

#13 is the §E retrieval budget+fusion item. Both edit `domain.py`. Overlap zones:

- **`skill_granular_select` (domain.py:851+).** §E (#13) adds a fused-score gate so the spare slot
  deepens the top skill. My #9 now *calls* `skill_granular_select` over Stage B survivors (§4.1). These
  **compose cleanly** as long as #13 keeps the signature `skill_granular_select(ranked, k) ->
  (list, list)`. **Coordination ask:** if #13 changes that signature (e.g. adds a `score_gate` param),
  my call site (domain.py:601 post-rewrite) must pass it too. Recommend #13's signature stays stable, or
  #13 lands its `skill_granular_select` change first and I rebase onto it.
- **Call-site block domain.py:597-602.** I rewrite this (filter→diversity). #13 touches k resolution
  (`pool_size`, the Tier-2 explicit-k path) *upstream* of this block (domain.py:433 and proxy_apply). Low
  overlap, but the two PRs both edit the same function `retrieve_domain_fragments` — **expect a merge
  conflict in the 560-623 region**; resolve by applying #9's selection rewrite inside #13's k-aware
  structure. Neither change is semantically incompatible.
- **No conflict on the doc-cap, server slots, presets, health, breaker** — those files are #9-only
  (except presets, where #13 may add tag-filter knobs; YAML merges are trivial).

**Also coordinate with #14** (env/port/listener drift): #14 owns the rerank launcher ports + which
listener serves localhost + the health surface. My `start_rerank_server.py` slot flags and `health_router`
reranker probe touch the same files. Verified here that localhost=native uvicorn and rerank=47952 single
listener (§0); #14 should confirm prod has no co-listening container before the A/B.

---

## 7. Sequencing & dependencies

- **Depends on #1 (LOG_LEVEL/§A) + #2 (passthrough telemetry/§B)** to *observe* the fix: the Stage B
  `timeout`→`hit` flip and kept/dropped ids only become measurable once those land. Not a hard code
  dependency — #9 can be written in parallel — but the on-vs-off A/B that validates §D needs them.
- **Internal order:** §C doc-cap (3.1) → §C slots+bound (3.2/3.3) → §D selection (4.1) → §D threshold
  (4.2) → §D health+breaker (4.3). Ship as one PR.
- **Validation gate (Risk #2):** before declaring Stage B effective, run a Stage-B-on-vs-off A/B with a
  *fast* (doc-capped, 8-slot) reranker so it HITs, on the gold set. §D must show non-regression vs
  deterministic-only. Keep `LM_ASSIST=arbitrate` gated to GPU presets (already so).
- **§8 fragment-atomicity reslice is a CORPUS-batch prerequisite for the K-sweep (#13), NOT for #9.**
  #9 ships against the current corpus; the doc-cap is the runtime floor that makes that safe.

## 8. Open items deferred (not blocking #9)

- Truly-shared bounded queue across the two `FragmentScorer` singletons (vs the shared-knob bound) —
  defer unless the A/B shows residual 8+1 oversubscription.
- Stage A (`RUNTIME_RERANK_*`) is unset live (`reranked=False`) — out of #9 scope; flagged for #14.
- Synchronous /health probe vs rolling-counter — recommend counter (no /health latency); revisit if a
  direct liveness probe is wanted.
