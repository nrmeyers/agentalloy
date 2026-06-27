# Plan 13 — Retrieval engine: k-cap, small-k selection, contract_tags filter, pool gate (§E, code only)

**Batch:** CODE (pure code/config; **no** SkillVersion bump, **no** corpus re-embed, **no** image
rebuild). `needs_reembed: false`.
**Source:** v3.11.1 @ `/home/nmeyers/dev/claude/agentalloy/.claude/worktrees/feedback`
**Authority:** PLAN-OF-ATTACK.md §E (+ owner decisions on K, atomicity ordering, single-topic budget).
**Shares files with:** **#9** (Stage B) — both edit `src/agentalloy/retrieval/domain.py`, and the conflict
is concentrated at the selection call-site (domain.py:597-602). See *Coordination* below.
**Coordinates with:** **#14** (corpus hygiene / benchmark-pack exclusion + poison tags) and **#15**
(fragment atomicity — gates the empirical K sweep).

This item is the **engine** half of §E. Excluded (do NOT touch here): corpus content / poison tags
(→#14), Stage B selection internals `_maybe_lm_arbitrate`/`lm_assist.py` (→#9), fragment reslice (→#15).

---

## Locked decisions (per PLAN-OF-ATTACK §9 — these override any divergent value below)

- **D3 / build+ship k = 4. LOCKED as the shipped default** (env-knob exposed). The 3–6 sweep runs **after** #15-B reslice on the clean corpus (decision gate D3); `DEFAULT_MAX_TOKENS_BY_PHASE` moves in lockstep (E2: 2048→4096). ✓ matches this plan.
- **D2 / benchmark exclusion** — §9's intent ("benchmark packs must not pollute prod retrieval") is satisfied via **#14 Option B** (`category: benchmark` + this plan's dormant E6 pool-gate), NOT physical deletion (deletion breaks `gold_hit 18/18`). E6 ships **dormant** (`AGENTALLOY_PHASE_GATE=off`); flip to `on` only after #14's recat is re-embedded and verified. Reserved literal **`benchmark`** must be byte-identical between this plan's allowlist/env and #14's `category:`.

---

## 0. Scope summary (five concrete changes)

| # | Change | File(s) | Default ships as |
|---|--------|---------|------------------|
| E1 | Raise `DEFAULT_K_BY_PHASE[build,ship]` 2→**4**, make per-phase k env-configurable | `api/compose_models.py`, `api/retrieve_models.py` | active (k=4) |
| E2 | Move `DEFAULT_MAX_TOKENS_BY_PHASE[build,ship]` 2048→**4096** in lockstep + env knob | `api/compose_models.py`, `orchestration/compose.py` | active (4096) |
| E3 | Pass **explicit k** on the Tier-2 `compose_request_from_contract` path (proxy_apply.py:167 never passes k) | `api/compose_models.py`, `api/proxy_apply.py` | inherits phase default (4) |
| E4 | **Fused-score deepen-gate** in `skill_granular_select` (spare small-k slot deepens top skill unless skill N+1 is within a relative band) | `retrieval/domain.py` | **inert** (band=0.0); recommend 0.85 post-sweep |
| E5 | Promote `contract_tags` from soft BM25 steer to a soft **domain filter** (intersect-then-fallback) | `retrieval/domain.py` | active (env kill-switch on) |
| E6 | Restore a **phase/category pool gate** (mechanism; benchmark exclusion lands when #14 categorizes) | `retrieval/domain.py` | **dormant** (gate=off); #14 flips on |

Everything that changes *observable selection for every compose by default* (E1/E2/E5) is paired with a
test update; everything risky-without-the-sweep (E4) or dependent on #14 (E6) ships **dormant behind a
flag** so #13 is a clean, green code-batch landing today, with the mechanism in place to activate later.

---

## 1. Owner decisions baked in / still needed

**Already decided (apply directly):**
- K=2 is a stale pre-corpus-improvement setting → **raise to 4** and make it env-configurable + testable.
- The empirical **K sweep runs AFTER #15** atomicity (measuring K against a mis-sliced corpus is noise).
  Therefore #13 ships **k=4 as a reasoned default** (the on-domain skill already holds 4-6 fragments in
  the pool but currently gets 1 slot at k=2) + the env knob `AGENTALLOY_K_<PHASE>` so the post-#15 sweep
  varies k with **zero code change**. A follow-up may bump the default once the sweep confirms 4 vs 5 vs 6.
- Single-topic token budget **~350-400 tok** is the shared number for #15 lint / §C doc-cap / reslice —
  **not consumed here** (retrieval engine does not read fragment token budgets), noted only so E2's
  `max_tokens` raise is understood as orthogonal (output budget, not fragment-size budget).

**DECISIONS NEEDED before/at coding (flag to owner):**
1. **D1 — recommended build/ship k value.** Plan ships **4**. Confirm (vs 5/6). Used by the test asserts.
2. **D2 — E4 deepen-band default.** Plan ships **0.0 (inert, behavior-preserving)** with recommended
   tuned value **0.85** set via `AGENTALLOY_DEEPEN_BAND` during the post-#15 sweep. Confirm we do NOT
   activate the gate by default in this code batch (activating changes every compose's selection before
   the sweep validates it).
3. **D3 — E5 contract_tags soft-filter default.** Plan ships **on** (`AGENTALLOY_CONTRACT_TAG_FILTER=on`)
   because empty-fallback makes it safe and it is the headline symptom fix ("1 react + 1 snowflake" →
   "react+ui only"). Kill-switch exists for the §E A/B. Confirm on-by-default is acceptable, or ship off
   and flip in #14's PR.
4. **D4 — E6 pool-gate posture + benchmark category name.** Plan ships the gate **dormant**
   (`AGENTALLOY_PHASE_GATE=off`, current phase-agnostic behavior preserved) and exposes a **product-
   category allowlist** keyed on a reserved category name that #14 will assign to benchmark packs.
   Confirm the reserved category string (**`benchmark`** proposed) so #14 and #13 agree on the literal.

---

## 2. Current behavior (verified against source)

- **Phase k table** `api/compose_models.py:27-35` → `build:2, ship:2, sdd-fast:2, qa/spec/design/intake:4`.
  `ComposeRequest.resolved_k()` (`compose_models.py:110-112`) and `RetrieveRequest.resolved_k()`
  (`api/retrieve_models.py:37-39`) both index `DEFAULT_K_BY_PHASE[self.phase]` directly.
- **Max-tokens table** `api/compose_models.py:41-49` → `build/ship/sdd-fast:2048`, rest `4096`. Consumed
  at `orchestration/compose.py:210,304` as `recommended_max_tokens=DEFAULT_MAX_TOKENS_BY_PHASE[req.phase]`.
- **k flows** `orchestration/compose.py:319` `k=req.resolved_k()` → `retrieve_domain_candidates(..., k=…)`.
- **Tier-2 path** `api/proxy_apply.py:167` `compose_request_from_contract(contract, legs="domain")`
  builds a `ComposeRequest` (`compose_models.py:138-162`) with **no k** → falls to phase default (today 2).
- **contract_tags is steer-only** `retrieval/domain.py:99-106` `_resolve_bm25_query` joins tags into the
  BM25 query string; **never filters the candidate pool**. Pool hydration passes `domain_tags` (the API
  filter) but `contract_tags` is invisible to `get_active_fragments`.
- **Pool is phase-agnostic** `retrieval/domain.py:518-522` and `:309-313` (fallback) call
  `get_active_fragments(categories=None, phases=None, …)`; the dense/bm25 searches pass `categories=None,
  phases=None` (`:491-497`, the dense leg likewise). The old `phase_to_categories`/`_PHASE_TO_CATEGORIES`
  gate was removed in #184 (b5409b5) and the dead map deleted in #186 (5b7d0b5) — **not present today**.
- **skill_granular_select** `retrieval/domain.py:851-950` takes `(ranked, k)` only — **no scores**. At
  k=2: `depth = k//2 = 1` (top skill gets 1 slot via Stage 1), Stage 2 round-robin spends the other slot
  on the **2nd distinct skill** regardless of how far below it ranks. Called at **:602** (main) and
  **:331** (`_bm25_fallback_result`). `scores_by_id` (fused-rank score in (0,1], descending) is already
  built at **:539-545** (main) / **:318-324** (fallback) — in scope at both call-sites.

---

## 3. Detailed changes

### E1 — raise build/ship k to 4 + per-phase env override

**`api/compose_models.py`**

1. Edit the table (`:27-35`):
   ```python
   DEFAULT_K_BY_PHASE: dict[str, int] = {
       "build": 4,      # was 2 — k=2 was a pre-corpus-improvement default (POC §15.7); the
       "ship": 4,       #         on-domain skill holds 4-6 fragments but won 1 slot. Revisited #13.
       "sdd-fast": 2,   # compressed action pass stays tight
       "qa": 4, "spec": 4, "design": 4, "intake": 4,
   }
   ```
   Update the §15.7 comment block (`:21-24`) to note build/ship were revisited upward in #13 and the
   final value is confirmed by the post-#15 K sweep via the env knob.
2. Add a resolver near the table (after `:49`), reading a per-phase override (the K-sweep knob):
   ```python
   import os as _os  # module-level, top of file

   def _phase_k(phase: str) -> int:
       """Phase-default k with an optional ``AGENTALLOY_K_<PHASE>`` env override (the K-sweep knob).
       Override is clamped to [1, 50]; malformed/empty falls back to the table. ``-`` → ``_`` so
       ``sdd-fast`` reads ``AGENTALLOY_K_SDD_FAST``."""
       raw = _os.environ.get(f"AGENTALLOY_K_{phase.upper().replace('-', '_')}")
       base = DEFAULT_K_BY_PHASE[phase]
       if raw:
           try:
               return max(1, min(50, int(raw)))
           except ValueError:
               return base
       return base
   ```
3. `ComposeRequest.resolved_k` (`:110-112`) before→after:
   - before: `return self.k if self.k is not None else DEFAULT_K_BY_PHASE[self.phase]`
   - after:  `return self.k if self.k is not None else _phase_k(self.phase)`

**`api/retrieve_models.py:37-39`** — same swap, importing `_phase_k` from `compose_models`
(it already imports `DEFAULT_K_BY_PHASE` at `:10`; add `_phase_k`):
- after: `return self.k if self.k is not None else _phase_k(self.phase)`

### E2 — move max_tokens up in lockstep + env knob (Risk #5 guard)

**`api/compose_models.py:41-49`**: `build`/`ship` 2048→**4096** (match qa/spec/design); `sdd-fast` stays
2048. Add a symmetric resolver:
```python
def _phase_max_tokens(phase: str) -> int:
    raw = _os.environ.get(f"AGENTALLOY_MAX_TOKENS_{phase.upper().replace('-', '_')}")
    base = DEFAULT_MAX_TOKENS_BY_PHASE[phase]
    if raw:
        try:
            return max(256, int(raw))
        except ValueError:
            return base
    return base
```
**`orchestration/compose.py:210` and `:304`** before→after:
- `recommended_max_tokens=DEFAULT_MAX_TOKENS_BY_PHASE[req.phase]`
- → `recommended_max_tokens=_phase_max_tokens(req.phase)` (import `_phase_max_tokens` from
  `compose_models`; the import line at `compose.py:19` already pulls `DEFAULT_MAX_TOKENS_BY_PHASE`).

> Rationale (Risk #5): raising k without raising the output budget re-introduces the T8 truncation/ramble
> the k=2 default was chosen to avoid. Lockstep is mandatory.

### E3 — pass explicit k on the Tier-2 path

**`api/compose_models.py`** `compose_request_from_contract` (`:138-162`) — add a `k` param:
```python
def compose_request_from_contract(
    contract: Contract, *, legs="both", requesting_agent="post_tool_use",
    k: int | None = None,                       # NEW
) -> ComposeRequest:
    return ComposeRequest(..., k=k, ...)          # thread into the constructed request
```
**`api/proxy_apply.py`** — add a Tier-2 k resolver and pass it at the call-site:
```python
def _tier2_k() -> int | None:
    """Explicit per-work-item k for the Tier-2 domain leg. ``AGENTALLOY_TIER2_K`` overrides; None →
    the phase default (post-E1, build=4). Lets the Tier-2 leg be tuned independently of /compose."""
    raw = os.environ.get("AGENTALLOY_TIER2_K")
    if raw:
        try:
            return max(1, min(50, int(raw)))
        except ValueError:
            return None
    return None
```
`proxy_apply.py:167` before→after:
- `domain_req = compose_request_from_contract(contract, legs="domain")`
- → `domain_req = compose_request_from_contract(contract, legs="domain", k=_tier2_k())`

(With `AGENTALLOY_TIER2_K` unset, Tier-2 now resolves to the **new** phase default 4 — the headline fix
for the thin 2-fragment build contract on the live native passthrough path.)

### E4 — fused-score deepen-gate in `skill_granular_select` (ships inert)

**`retrieval/domain.py`** — new signature (keyword-only, fully back-compatible):
```python
def skill_granular_select(
    ranked: list[ActiveFragment], k: int, *,
    scores_by_id: dict[str, float] | None = None,
    deepen_band: float = 0.0,
) -> tuple[list[ActiveFragment], list[str]]:
```
Add a module constant + env read near the other tunables (`:108-116`):
```python
_DEEPEN_BAND_DEFAULT = 0.0  # 0.0 == legacy breadth-first; recommend 0.85 after the post-#15 K sweep
def _deepen_band() -> float:
    try:
        return max(0.0, min(1.0, float(_os.environ.get("AGENTALLOY_DEEPEN_BAND", _DEEPEN_BAND_DEFAULT))))
    except ValueError:
        return _DEEPEN_BAND_DEFAULT
```
**Gate logic** (replaces the Stage-2 `rotation` construction at `:918-923`): partition the non-top skills
into **near** (lead score within band of the top skill) and **far** (below band), using the *original*
`skill_queues` (immutable) for lead-score lookup:
```python
top_lead = (scores_by_id or {}).get(skill_queues[skills_ranked[0]][0].fragment_id, 0.0)
threshold = deepen_band * top_lead if (scores_by_id and deepen_band > 0.0) else 0.0
def _lead(sid): return (scores_by_id or {}).get(skill_queues[sid][0].fragment_id, 0.0)
siblings = skills_ranked[1:] if depth and len(skills_ranked) > 1 else skills_ranked
near = [s for s in siblings if threshold == 0.0 or _lead(s) >= threshold]
far  = [s for s in siblings if threshold > 0.0 and _lead(s) < threshold]
```
Then run the selection in **4 stages**:
- **Stage 1** depth guarantee for top skill (unchanged, `:907-916`).
- **Stage 2** round-robin over **`near`** (the existing Stage-2 loop body, iterate `near`).
- **Stage 3** deepen the **top** skill's leftovers (existing `:941-948`).
- **Stage 4** (NEW) if still `< k`, round-robin over **`far`** (below-band siblings) so k is always filled
  — far siblings are a *last resort*, admitted only after top depth is exhausted.

Semantics: `deepen_band=0.0` ⇒ `far=[]`, `near=` all siblings ⇒ **byte-for-byte identical to today**
(every current `test_retrieval_domain.py` case passes unchanged). `deepen_band=0.85` at k=2 ⇒ the spare
slot deepens the top skill unless skill #2's lead fragment ranks within ~top-7 of a 50-pool (score ≥ 0.85
under the rank-derived `1 - i/n` scoring). The rank-derived score is dense across the full pool (not just
the selected set), so the band is meaningful, not inert.

**Both call-sites** thread the new kwargs:
- `:602` → `selected, skills_ranked = skill_granular_select(ranked, k, scores_by_id=scores_by_id, deepen_band=_deepen_band())`
- `:331` (`_bm25_fallback_result`) → `selected, _ = skill_granular_select(ranked, k, scores_by_id=scores_by_id, deepen_band=_deepen_band())`

### E5 — contract_tags as a soft domain filter (intersect-then-fallback; ships on)

**`retrieval/domain.py`** — new helper + kill-switch:
```python
def _contract_tag_filter_enabled() -> bool:
    return _os.environ.get("AGENTALLOY_CONTRACT_TAG_FILTER", "on").strip().lower() != "off"

def _soft_tag_filter(ranked, contract_tags):
    """Intersect the fused pool with fragments carrying >=1 contract tag; fall back to the full pool
    when the intersection is empty (process-vocab contracts whose tags match no domain skill must not
    empty retrieval). domain.py already hydrates frag.domain_tags."""
    if not contract_tags:
        return ranked
    want = {t.lower() for t in contract_tags}
    keep = [f for f in ranked if want & {t.lower() for t in f.domain_tags}]
    return keep if keep else ranked
```
**Main path** — insert immediately after `ranked`/`scores_by_id` are built (`:545`, before
`eligible_count` at `:562`), so Stage A/B (#9) and selection all operate on the narrowed pool:
```python
if _contract_tag_filter_enabled():
    ranked = _soft_tag_filter(ranked, contract_tags)
```
**Fallback path** (`_bm25_fallback_result`) — same insert after `:324`, before its
`skill_granular_select` call at `:331`.

> This is additive to (not a replacement for) the existing BM25 steer in `_resolve_bm25_query` — tags
> still bias retrieval AND now also softly filter the hydrated pool. Empty-fallback is the safety valve
> that protects `legs="domain"` contracts carrying only process tags.

### E6 — restore the phase/category pool gate (ships dormant; #14 activates)

The #184 A/B that retired the gate as "performance-neutral" **predates benchmark-pack contamination**
(Risk #4). The benchmark packs (snowflake/data-engineering/vue/temporal/fastapi) share
`category: engineering` with React, so the *old* per-phase map cannot separate them — the engine needs a
**product-category allowlist** that excludes a reserved `benchmark` category which **#14** assigns at the
pack level (corpus batch).

**`retrieval/domain.py`** — add (dormant by default):
```python
# Categories that belong in the production candidate pool. Benchmark-only packs are tagged
# category="benchmark" by #14 (corpus); the gate drops them. Off by default (phase-agnostic, today's
# behavior) until #14's re-categorization + re-embed lands — then flip AGENTALLOY_PHASE_GATE=on.
_PRODUCT_CATEGORIES: tuple[str, ...] = (
    "engineering", "design", "tooling", "quality", "ops", "operational", "review",
)
def _pool_categories() -> list[str] | None:
    if _os.environ.get("AGENTALLOY_PHASE_GATE", "off").strip().lower() == "on":
        return list(_PRODUCT_CATEGORIES)
    return None  # phase-agnostic (current behavior)
```
Wire `categories=_pool_categories()` (replacing the hardcoded `categories=None`) at the **three** pool
reads in the main path — dense search (`:491` area), bm25 search (`:491-497`), and
`get_active_fragments` (`:518-522`) — and the **two** in `_bm25_fallback_result` (`:301-307`, `:309-313`).
`phases=` stays `None` (the allowlist, not phase_scope, is the benchmark lever). When the env is unset the
function returns `None` → **identical to today**, so this is a true no-op until #14 + the flip.

> Coordination with #14: #14 (a) re-categorizes the five benchmark packs to `category: benchmark` (or
> excludes them from the bundled corpus entirely — either satisfies the gate), (b) flips
> `AGENTALLOY_PHASE_GATE=on` in the GPU/CPU presets, both in the same re-embed PR. If #14 instead removes
> benchmark packs from the production index, E6 stays dormant and harmless. The literal `benchmark`
> category string is **D4**.

---

## 4. Coordination with #9 (shared `retrieval/domain.py`)

**Conflict zone = the selection block `domain.py:564-607`.** #9 edits Stage B
(`_maybe_lm_arbitrate`/`lm_assist.py`, `keep_threshold`, the `lm_selected is not None` branch and its
fall-through to `skill_granular_select` at `:602`). #13 edits the **`else` branch** call at `:602` (adds
kwargs) and inserts the E5 soft-filter just above (`:545-562`) and E6 `categories=` at the pool reads
(`:491-522`).

**Merge protocol:**
- E5's filter and E6's `categories=` are **above** the Stage A/B block → they reshape the `ranked` pool
  that #9's Stage B then scores. Land **#13's pool-shaping first**, then #9 rebases — Stage B scores the
  already-filtered/gated pool (desirable; no semantic conflict, only line adjacency).
- The `:602` call is the literal merge point: #9 keeps the `if lm_selected is not None: … else:` shape;
  #13 only changes the **else** call's argument list. Apply #13's kwargs to whichever line #9 leaves as
  the deterministic fall-through. Trivial textual merge if sequenced; flag for a 2-line manual resolve if
  parallel.
- No shared function bodies otherwise: #13 owns `skill_granular_select`/`_soft_tag_filter`/`_pool_categories`/
  `_deepen_band`; #9 owns `_maybe_lm_arbitrate`/`_LMArbitrationDetail`/`lm_assist.py`.

**Recommended order:** #13 lands before #9 (pool-shaping is a prerequisite the §E note calls out: "Stage B
cannot inject React fragments that never enter the pool — fix budget/fusion first").

---

## 5. Tests

**`tests/test_compose_contract.py`** (update existing asserts at `:94-100`):
- `build`/`ship` resolved_k 2→**4**; keep `qa/design/intake==4`, `sdd-fast==2`, explicit `k=8` override.
- NEW `test_phase_k_env_override`: `monkeypatch.setenv("AGENTALLOY_K_BUILD","6")` →
  `ComposeRequest(phase="build").resolved_k()==6`; malformed (`"x"`)→4; clamp (`"99"`)→50.
- NEW `test_tier2_compose_passes_explicit_k`: `compose_request_from_contract(c, legs="domain", k=3).k==3`;
  with `AGENTALLOY_TIER2_K` set, assert `proxy_apply._tier2_k()` returns it (clamped); unset → None.

**`tests/test_retrieval_domain.py`** (existing `skill_granular_select` suite at `:191-327` must still pass
unchanged — proves E4 default is byte-for-byte legacy):
- NEW `test_deepen_gate_deepens_top_when_sibling_far`: pool = skill A (frags at ranks 0,1,2) + skill B
  (lead at rank ~30 of 50), `scores_by_id` rank-derived, `k=2`, `deepen_band=0.85` → selection is **2× A**,
  not A+B.
- NEW `test_deepen_gate_keeps_breadth_when_sibling_near`: skill B lead at rank 1 (within band) → A+B
  (breadth preserved).
- NEW `test_deepen_gate_band_zero_is_legacy`: `deepen_band=0.0` reproduces the current `(ranked,k)` output
  exactly on a fixture where the gate would otherwise fire.
- NEW `test_deepen_gate_fills_k_when_all_far`: all siblings below band but top skill shallow → Stage 4
  fallback still returns k fragments (no under-fill).
- NEW `test_soft_tag_filter_intersect`: ranked = [react-*, snowflake-*], `contract_tags=["react"]` →
  snowflake dropped; `test_soft_tag_filter_empty_fallback`: `contract_tags=["nonexistent"]` → full pool
  returned; `test_soft_tag_filter_disabled`: `AGENTALLOY_CONTRACT_TAG_FILTER=off` → no filtering.

**`tests/test_retrieve.py` / hermetic e2e** (per §E test strategy — drive `retrieve_domain_candidates`
with a seeded store): k-monotonicity (`len(domain_fragments) == resolved_k` when pool ≥ k); top skill
contributes ≥2 fragments at k=4 with the gate active; `_pool_categories()` returns `None` when
`AGENTALLOY_PHASE_GATE` unset and the product list when `on` (mechanism test — full benchmark-exclusion
regression is #14's).

**`tests/test_config_consistency.py`** (Risk #7 — enumerate every new knob so it can't drift): assert the
new env knobs are documented in `.env.example`/presets template: `AGENTALLOY_K_*`, `AGENTALLOY_TIER2_K`,
`AGENTALLOY_DEEPEN_BAND`, `AGENTALLOY_CONTRACT_TAG_FILTER`, `AGENTALLOY_PHASE_GATE`. Add a guard that
`DEFAULT_K_BY_PHASE` and `DEFAULT_MAX_TOKENS_BY_PHASE` keys are identical and cover the full `Phase`
Literal (the existing module comment promises this; make it enforced).

---

## 6. Files touched (conflict-detection manifest)

**Modified:**
- `src/agentalloy/api/compose_models.py` — E1/E2/E3 (tables, `_phase_k`, `_phase_max_tokens`,
  `compose_request_from_contract` k param). *Siblings on this file:* #14 (sdd density §G touches it too —
  G adds the design exit-gate count; different region, low risk), #7/#8? none in §A-§D.
- `src/agentalloy/api/retrieve_models.py` — E1 (resolved_k via `_phase_k`). *No known sibling.*
- `src/agentalloy/api/proxy_apply.py` — E3 (`_tier2_k`, call-site). *Sibling:* #9 does **not** touch this
  (it lives in proxy/domain); telemetry items (#? §B) touch `proxy_passthrough_router.py`, not this. Low risk.
- `src/agentalloy/orchestration/compose.py` — E2 (`recommended_max_tokens`). *Sibling:* #9 may read Stage B
  telemetry fields here (`:244-287`) — different region (telemetry record), low risk.
- `src/agentalloy/retrieval/domain.py` — **E4/E5/E6. SHARED WITH #9** (see §4). Highest conflict surface.

**New (none — all changes are edits or test additions).**

**Tests:** `tests/test_compose_contract.py`, `tests/test_retrieval_domain.py`, `tests/test_retrieve.py`,
`tests/test_config_consistency.py`.

**NOT touched (guard against scope creep):** `retrieval/lm_assist.py`, `signals/classifier.py`,
`api/health_router.py` (→#9); any `_packs/**` YAML (→#14/#15); `storage/vector_store.py` (its
`search_similar`/`search_bm25` already accept `categories=` at `:406,:481` — no signature change needed).

---

## 7. Sequencing & risk

- **Order within batch:** E1+E2+E3 (compose_models/proxy/compose — independent, mechanical) can land
  first; E4+E5+E6 (domain.py) land together and **before #9** (§4). 
- **Risk: medium** (E4/E6 dormant-by-default de-risks; E1/E5 change live selection but are the intended
  symptom fix with test coverage + empty-fallback). 
- **Effort: L** (5 changes across 5 source files + 4 test files; E4 gate logic is the only non-trivial
  algorithm).
- **K sweep ordering (hard):** the *empirical* tuning of the final k and `deepen_band` runs **after #15**
  re-slices the corpus. #13 ships the defaults + env knobs that make that sweep a config exercise, not a
  code change. Do not present post-#15 sweep numbers as part of #13.
- `needs_reembed: false` — verified: no pack/SkillVersion/embedding touch; pure engine code + env knobs.
