# AgentAlloy — Plan of Attack

Synthesis of 8 investigations into a single, dependency-ordered remediation plan.
Author: synthesis lead. Audience: AgentAlloy maintainers.

---

## 1. Executive Summary

Six reported issues collapse into **three coupled subsystems plus two independent tracks**:

1. **Observability is dark.** `LOG_LEVEL` is read into `Settings` but never applied — the
   systemd/launchd/container units launch `uvicorn agentalloy.app:app` directly, bypassing the
   only `logging.basicConfig` (in `__main__.py`), so every `agentalloy.*` logger sits at WARNING.
   Separately, the **native Anthropic passthrough surface** (`/proj/{token}/v1/messages`) — which
   the proxy-only migration made the live Claude Code transport — **never writes a CompositionTrace**,
   so `composition_traces` is empty and `/telemetry/*` returns nothing. We have been root-causing
   every other issue **blind and unmeasured**. These two fixes are cheap, isolated, and **must land
   first** because they are the instruments for everything below.

2. **Stage B reranker is both slow AND ineffective-by-design.** It times out at 1500 ms on ~100%
   of calls because the client fans 12 concurrent requests at a server with **only 4 KV slots**
   (no `--parallel` flag anywhere) — a pure oversubscription bug, GPU-independent, fixed by capping
   the client pool to slot count (probe: 1506 ms → 785 ms). But even when it HITs, Stage B **does not
   reorder** (it filters at an inert `keep_threshold=0.05` while keeping fusion order) and on a HIT it
   **bypasses `skill_granular_select`**, i.e. its net effect today is "turn off diversity selection
   for no benefit." Fixing latency alone risks a *quality regression*; latency and selection logic
   must ship together.

3. **The thin 2-fragment build contract is NOT a corpus gap.** Three throttles stack:
   `DEFAULT_K_BY_PHASE["build"]=2` hard-caps build/ship to 2 skills; the candidate pool is polluted
   with benchmark-only packs (snowflake/data-engineering/vue/temporal/fastapi) that out-rank React;
   and multi-tag BM25 fusion dilutes a named framework tag (`react`, 179 fragments) to **zero
   fragments** while two tags actively poison results (`frontend` indexed in backend prose,
   `calendar` only in airflow/temporal). The richest packs (react/typescript/ui-design/testing) exist
   and are individually reachable. The fixes are budget + fusion + soft tag-filter + corpus hygiene,
   plus 2–3 genuinely-missing skills (`vite`, frontend `calendar`/date-grid, optional `vitest`).
   The **build-contract density** issue is the workflow-prose face of the same k=2 cap: design emitted
   one monolithic 7-tag contract, so 5 of 7 tech surfaces got nothing — fixed by forcing
   one-contract-per-task prose sized to k=2 (proven 4x coverage with the reranker disabled).

4. **Two independent tracks** ride alongside: the passthrough telemetry write (track 1 above) and a
   **deterministic human-in-the-loop approval gate** at spec→design and design→build (a new
   `approval_recorded` predicate + `agentalloy approve` CLI + an unconditional `--force` carve-out).
   The approval gate touches none of the Stage B / retrieval / telemetry code.

**Bottom line ordering:** logging + passthrough-telemetry (instruments) → Stage B latency+selection
(coupled) → retrieval budget+fusion (the real symptom fix) → corpus authoring + sdd density prose
(pack-gated, one re-embed) → approval gate (independent, anytime). Two findings note the live env
has **drift** (a container listener `LM_ASSIST=off` vs native `LM_ASSIST=arbitrate` on the same port,
and benchmark packs co-indexed with product skills) that must be reconciled before any Stage B or
retrieval measurement is trusted.

---

## 2. Per-Area Sections

### A. LOG_LEVEL ignored — app loggers pinned at WARNING  *(finding #7)*

- **Problem:** `LOG_LEVEL=DEBUG` in the EnvironmentFile is parsed into `Settings.log_level` but
  INFO/DEBUG app diagnostics are invisible in systemd, launchd, and container deployments.
- **Root cause:** The units run `uvicorn agentalloy.app:app` directly. `agentalloy.app:app`
  (`app.py:277`, module-level `app = create_app()`) configures no logging. The only
  `logging.basicConfig` is in `__main__.py:15`, reachable only via `python -m agentalloy`, which the
  units do not use. uvicorn's `--log-level` (not even passed) only touches `uvicorn.*` loggers, never
  root, so it cannot rescue the `agentalloy.*` namespace. Container path hardcodes `--log-level info`
  (`container_runtime.py:613`), same blind spot.
- **Recommendation:** Add a `configure_logging()` helper called at the **top of `create_app()`**
  (`app.py:229`) that both `logging.basicConfig(level=lvl, ...)` and explicitly
  `logging.getLogger('agentalloy').setLevel(lvl)` (so it wins even when uvicorn/pytest already
  installed a handler). Because `app = create_app()` runs at import, this fires for **every**
  entrypoint and survives future ExecStart edits. Do **not** fix via ExecStart/`--log-level` — that
  rescues only systemd and re-couples observability to the entrypoint string. **Companion:** add an
  INFO line at the rerank/arbitrate decision site (the runtime proxy path logs no Stage B verdict at
  INFO/DEBUG today — only telemetry/warnings), so the logging fix has Stage B kept/dropped/outcome to
  surface in journalctl.
- **Effort:** S · **Risk:** low
- **Files:** `app.py`, `__main__.py`, `config.py`, `install/subcommands/enable_service.py`,
  `install/subcommands/container_runtime.py`
- **Test strategy:** Unit assert `getLogger('agentalloy').getEffectiveLevel()==DEBUG` after
  `create_app()` with `LOG_LEVEL=DEBUG`; `caplog` captures an `agentalloy.*` INFO record;
  config-consistency guard asserting the fix is **app-side** (not a `--log-level` flag in the unit);
  idempotency (exactly one root handler after repeated `create_app()`); manual on-host journalctl
  check of the `Phase transition` line.

### B. Passthrough telemetry write-gap  *(finding #6)*

- **Problem:** Every request through the live native transport (`/proj/{token}/v1/messages`) returns
  with **zero telemetry writes**; `composition_traces` and `/telemetry/{traces,savings}` are empty.
- **Root cause:** `passthrough_anthropic_messages` (`proxy_passthrough_router.py:222-228`) has no
  `vector_store` dependency and never imports `write_proxy_trace`. The OpenAI surface threads
  `vector_store` and calls `write_proxy_trace` on every exit path; the passthrough surface discards
  the already-computed `InjectOutcome.telemetry: ProxyComposeTelemetry`. Pure wiring gap.
- **Recommendation:** Fold the write into the **existing `on_status(status)` seam** (fires exactly
  once per forward, at the moment upstream status is known, on both streaming and non-streaming paths,
  where 2xx-gated cadence commit already lives). Add `get_vector_store` dep + `write_proxy_trace`
  import, resolve `phase`/`session_key`/`task_prompt` once, and on 2xx write
  `status='proxy_composed'` when the workflow block composed else `'proxy_passthrough'`. The outer
  try guards arg-construction; `write_proxy_trace` is internally soft-failing so it can never break the
  forward. Records `lm_assist_outcome='timeout'` — **which is what finally makes the Stage B timeout
  observable in the store.**
- **Effort:** M (mechanical) · **Risk:** low
- **Files:** `api/proxy_passthrough_router.py`, `tests/test_proxy_passthrough_native.py`
- **Test strategy:** TestClient e2e with a **real** store swapped into `app.state`: TC-passthrough
  (nothing composed → exactly 1 row, `proxy_passthrough`, session_key/source/repo asserted);
  TC-composed (`proxy_composed`, populated skill ids); TC-streaming-single-write (1 row at stream-open,
  not per chunk); TC-non2xx-no-row (529 → 0 rows). Reuse the canonical `query_traces` assertion from
  `tests/test_proxy_telemetry.py`.
- **Open scope:** error-path parity (OpenAI writes ERROR rows on 5xx/timeout; passthrough returns 502
  without invoking `on_status`) is **deliberately deferred** — confirm acceptable.

### C. Stage B reranker latency — slot oversubscription  *(finding #1)*

- **Problem:** Stage B times out at the 1500 ms budget on essentially every compose; the
  single-request signal-intent path (77 ms) never times out.
- **Root cause:** **Client/server slot oversubscription, not hardware.** `FragmentScorer` fans one
  `/v1/completions` per fragment across a **12-wide** ThreadPoolExecutor (`lm_assist.py:261`,
  `_MAX_CANDIDATES=12`) at a reranker llama-server with **only 4 KV slots** — no `--parallel/-np/-c`
  flag exists in the launcher, override, or any preset, so llama.cpp auto-picks `n_parallel=4`. 12→4
  forces 3 serial decode waves; uncancelled 12-wide cold = 1506 ms, sitting ~6 ms over budget →
  coin-flip cancel on every call. Pinning to the free 3060 fixed GPU saturation but not the 12>4
  mismatch (GPU-independent). Per-doc eval is genuinely fast (~120–160 ms).
- **Recommendation (revised — doc-cap-first, post-synthesis):** A second corpus measurement (3,313
  fragments: p50=149, p90=306, p99=433, **max=1,301** tok) reframes the fix. The dominant lever is a
  **client-side document cap**, which Stage B is *missing* — the embed path already strips oversized
  inputs at the 2048 ceiling (`domain.py:412`); the absence of the symmetric guard on the rerank path
  silently truncates the largest fragments at the current `-c`, degrading their scores. Apply in order:
  1. **Doc cap (primary).** Truncate each scorer document to **~550–600 tok** (~2,200 chars) before
     `scorer.score()` — covers p99 fully, only knifes genuine >p99 outliers, bounds the prompt to
     ~700 tok regardless of fragment size, and slashes per-request prefill (the real budget killer).
     Deterministic; mirrors the embed-ceiling guard. **A/B 400 vs 600 tok for verdict stability** on a
     labeled set (couple with §D threshold calibration); prefer the higher cap because a head-truncated
     fragment whose relevance sits in its tail is mis-scored. With docs capped, **even the current 4
     slots clear the budget** — server sizing becomes optimization, not the fix.
  2. **Explicit server slots.** Set `--parallel N` + `-c` in `start_rerank_server.py` (today neither is
     set → llama.cpp auto-picks `n_parallel=4`). Lean **`--parallel 8 -c 8192`** (1,024 tok/slot, 2
     waves, half the KV) over `--parallel 12 -c 12288`, and **do not assume linear slot scaling on the
     Vulkan build** (staggered slot-launch gaps suggest partial per-wave serialization — measure
     throughput-vs-slots before choosing 8 vs 12).
  3. **Bound the client pool to slots + share it.** Keep `max_workers ≤ n_parallel` from **one config
     knob** (so `_MAX_CANDIDATES` and slot count can't drift), and apply a **shared bound across the two
     `FragmentScorer` singletons** (compose Stage B + intent classifier) — else both firing 12-wide =
     24 concurrent on N slots re-oversubscribes regardless of the doc cap. This subsumes the original
     "cap the client pool" idea as the co-tenancy/drift guard, not the primary fix.
  Do **not** just raise the timeout (taxes every compose ~1.5 s). **Verify the actual runtime
  `n_ctx`/`n_parallel` and which listener serves localhost first** (env drift, #14 / risk #1) — a prior
  analysis reasoned from `-c 8192 --parallel 12`, which is **not** the launched config. The doc cap is
  also a **symptom-fix for under-sliced fragments** — see §8 (Fragment Atomicity), the root cause of the
  1,301-tok outlier; do the atomicity audit **before** the K=2→4 retrieval test.
- **Effort:** S–M (doc cap = S; server sizing + shared bound = M) · **Risk:** low
- **Files:** `retrieval/lm_assist.py:261,64,284-300`, `retrieval/domain.py:784,790`,
  `install/subcommands/start_rerank_server.py:163-171`, the rerank `override.conf`, `agentalloy.env`
- **Test strategy:** Unit assert FragmentScorer pool width ≤ configured `n_parallel`; e2e compose the
  build contract and assert `lm_assist_outcome=='hit'` not `'timeout'`; mutation: restore
  `max_workers=12` and confirm the timeout returns. Verify slot count at runtime by grepping the
  rerank journal for `n_parallel = N` rather than assuming.

### D. Stage B integration correctness — selection logic & observability  *(finding #2)*

- **Problem:** Even on a HIT, Stage B is **ineffective-to-negative**; its failure mode is invisible.
- **Root cause (compounding):** (1) Scores never **reorder** — `kept` preserves fusion order, used
  only as a ≥threshold gate (`domain.py:802-804`); the relevance ranking is discarded. (2)
  `keep_threshold=0.05` (`lm_assist.py:61`) is near-inert — a calibrated cross-encoder puts
  irrelevant ~0.1 / relevant ~0.9, so 0.05 prunes almost nothing; the **same** model is thresholded
  at **0.45** in the intent classifier (`classifier.py:131`) — a 9x inconsistency. (3) On a HIT, Stage
  B **replaces** `skill_granular_select` (`domain.py:597-602`) with plain fusion top-k, i.e. it turns
  off the measured-good diversity selection. (4) Degrade-to-dense is the correct safety floor but
  operationally invisible: `/health` reports config but never **probes** the reranker
  (`health_router.py:120-128`), so a stage timing out 100% of the time still reports `healthy`; the
  breaker re-arms every 60 s and times out forever. (5) Two independent FragmentScorer singletons
  (compose + intent) each with a 12-wide pool and its own latch hit the single server with no shared
  limit; `fut.cancel()` can't cancel running futures.
- **Recommendation:** Fix in order — **(A) selection:** either SORT `kept` by score before `[:k]`,
  OR drop sub-threshold fragments and feed survivors **into** `skill_granular_select` (preserve
  diversity, don't bypass). **(B) recalibrate** `keep_threshold` toward the classifier regime
  (~0.3–0.5) against a labeled fragment-relevance set; set `LM_ASSIST_KEEP_THRESHOLD` in the GPU
  presets + a config-consistency guard. **(C) observability:** flip `/health` to degraded + add a
  reranker probe when timeout dominates a rolling window; make the breaker escalate to a long/indefinite
  open after N cooldown-then-fail cycles. **(D) cleanups:** `per_req_s` strictly under the batch budget;
  fix the stale comment; shared bounded concurrency across the two scorers. **Net: Stage B will not
  improve quality until (A)+(B) land — verify with a Stage-B-on-vs-off A/B (with a fast reranker so it
  HITs) before declaring it effective.**
- **Effort:** M · **Risk:** medium
- **Files:** `retrieval/domain.py`, `retrieval/lm_assist.py`, `retrieval/rerank.py`,
  `signals/classifier.py`, `api/health_router.py`, `install/presets/{nvidia,radeon,apple-silicon}.yaml`
- **Test strategy:** Stub-scorer unit tests: HIT ordered by score; sub-threshold dropped + refill to k;
  all-below → []; DISABLED/TIMEOUT/ERROR fall through to `skill_granular_select` byte-for-byte
  (fail-open guard). Calibration test asserting the threshold drops a meaningful fraction on a labeled
  mixed set. Health test: timeout-dominant → degraded + reranker dependency appears. Breaker test:
  latch holds open beyond 60 s. End-to-end gold-hit A/B Stage-B on vs off.

### E. Retrieval pipeline — k-cap, polluted pool, contract_tags  *(findings #3 + #4)*

- **Problem:** The build contract returns ~2 fragments, and they are wrong (1 generic UI + 1 Snowflake
  data-warehouse fragment for a React task); React (the richest pack, a named tag) contributes **zero**
  fragments at any k.
- **Root cause (three throttles + a dead safety net):** (1) **k=2 hard cap** for build/ship
  (`compose_models.py:27-35`); the Tier-2 path `compose_request_from_contract(..., legs="domain")`
  (`proxy_apply.py:167-168`) never passes k, so it falls to the phase default. (2) The 50-pool is
  **polluted** — retrieval is phase/category-agnostic (`domain.py:472-497`; the gate removed as
  "performance-neutral" — but that A/B **predates benchmark-pack contamination**), and the live index
  co-indexes benchmark-only packs (snowflake/data-engineering/vue/temporal/fastapi) that out-rank
  React. `skill_granular_select` at k=2 sets depth=1 and spends the only other slot on the
  **2nd-ranked distinct skill** — frequently off-domain. (3) `contract_tags` is only a **soft BM25
  steer**, not a filter (`compose_models.py:152-155`); generic tags (`frontend`,`typescript`) match
  broadly and fail to suppress noise. **Poison tags:** `frontend` is indexed in backend skills' prose
  (→ fastapi/fastify); `calendar` exists only in airflow/temporal (→ backend cron). (4) The salvage
  stage (Stage B) is dead (timeout/latched), so the k=2 breadth selection always stands un-pruned.
  Genuinely missing skills: **no `vite`** (resolves to Vue), **no frontend `calendar`/date-grid** (the
  literal product), optional dedicated `vitest`.
- **Recommendation (highest-leverage first):** (1) **Raise build/ship k** (2→4) or pass explicit k on
  the Tier-2 path — the relevant skill already has 4–6 fragments in the pool but gets 1 slot; move
  `DEFAULT_MAX_TOKENS_BY_PHASE` up in lockstep. (2) **Fix small-k selection** — add a fused-score gate
  to `skill_granular_select` so the spare slot deepens the top skill unless skill N+1 is within a
  relative band. (3) **Promote `contract_tags` to a soft domain filter** (intersect-then-fallback;
  proven to convert "1 react + 1 snowflake" → "react+ui only", safe on process-tag contracts via empty
  fallback). (4) **Corpus hygiene** — exclude benchmark-only packs from the production index or restore
  a category gate, and **repair poison tags** (strip `frontend` from backend skills, down-weight
  cross-tier `calendar`). (5) **Boost framework-tier tags** in the BM25 query or retrieve-per-tag-and-
  merge so each named framework guarantees ≥1 candidate. **#1+#3 fix the symptom even with Stage B
  dead; #4/#5 are durable; Stage B (D/C) is the online safety net but cannot inject React fragments
  that never enter the pool — fix budget/fusion first.**
- **Effort:** L · **Risk:** medium
- **Files:** `api/compose_models.py`, `api/proxy_apply.py`, `retrieval/domain.py`,
  `orchestration/compose.py`, `retrieval/lm_assist.py`, `storage/vector_store.py`
- **Test strategy:** Hermetic e2e retrieval regression: compose the calendar build contract and assert
  ≥1 react-* skill, **0** of {snowflake-*, data-engineering-*, vue-*, temporal-*, fastapi-*}, and the
  top skill contributes ≥2 fragments at default k. Per-tag reachability (`['react']`→react-*,
  `['frontend']`→no backend). k-monotonicity (`domain_fragments == resolved_k`). Corpus lint (no
  backend skill carries `frontend`). Unit `skill_granular_select` at k=2 with a far-below skill#2
  asserting deepen-not-pull. A/B soft-filter vs BM25-steer over the gold set (no empty-retrieval
  regression on process-vocab contracts).

### F. Corpus authoring — missing frontend skills + poison-tag repair  *(finding #3, content side)*

- **Problem:** Two-to-three genuinely-absent skills and two mis-tagged backend packs poison frontend
  retrieval.
- **Root cause:** No `vite`, no frontend `calendar`/date-grid skill; `frontend` tag embedded in
  fastapi/fastify prose; `calendar` tag only in airflow + temporal skills.
- **Recommendation:** Author a `vite` skill (dev server, config, plugins, build, env, code-splitting)
  and a frontend `calendar`/date-grid/temporal-UI skill (the product IS calendar-month-view); optionally
  a dedicated `vitest` skill. Strip `frontend` from backend skills; remove/down-weight `calendar` on
  airflow/temporal. **All content changes require a SkillVersion bump + corpus re-embed + image rebuild
  (proxy is the container image).**
- **Effort:** M–L · **Risk:** medium (re-embed coordination)
- **Files:** `src/agentalloy/_packs/` (new `vite/`, `calendar`/date-grid), `_packs/fastapi/`,
  `_packs/fastify/` (strip tag), `_packs/temporal/temporal-schedules-and-timers.yaml`,
  `_packs/data-engineering/data-engineering-airflow-best-practices.yaml`
- **Test strategy:** `['vite']`→a vite skill, `['calendar']`→a frontend calendar skill after authoring;
  corpus integrity + bundled-corpus tests; re-run the retrieval regression from §E.

### G. Build-contract density — sdd-design prose granularity  *(finding #5)*

- **Problem:** Design handed build a single monolithic 7-tag contract spanning all 8 tasks; at k=2,
  5 of 7 tech surfaces get zero fragments.
- **Root cause:** §6 ("one build contract per task") is **soft framing**, not a hard MUST, never
  states the k=2 cap, and §3 vertical-slicing + §6's 4-tag example actively model the multi-tag
  dilution that starves k=2. The design exit-gate only checks `artifact_exists build/*.md` (≥1), so
  the monolith passed clean. **Decomposition alone gives ~4x coverage with the reranker disabled** —
  this is a prose/template fix, not an engine change.
- **Recommendation:** (1) §3 — add that build contracts are finer than design slices and must center
  ONE dominant tech surface. (2) §6 — promote to a hard **MUST** ("ONE build contract per task —
  never a whole-feature contract"), state the ~2-skill cap, replace the 4-tag example with
  "one dominant + at most one adjacent" (`[typescript, pure-functions]` etc.), add an explicit
  anti-pattern forbidding the all-tech contract. (3) `sdd-build.yaml` template — annotate `domain_tags`
  and tighten the `## Task` placeholder to a single surface. (4) **Tighten the design exit-gate** so
  build-contract count ≥ tasks.md task count (or ≥ distinct tech surfaces) — enforce, don't merely
  advise. Orthogonal to Stage B (these composes ran `lm_outcome=disabled`).
- **Effort:** M · **Risk:** low
- **Files:** `_packs/sdd/sdd-design-and-planning.yaml`, `_packs/sdd/sdd-build.yaml`,
  `api/compose_models.py`
- **Test strategy:** Regression on the compose comparison (monolith ≤2 on-surface skills; each
  decomposed body ≥1 matching skill). Golden test asserting §6 contains the k=2 cap statement +
  single-dominant-tech rule + template comment (prose-drift guard). Gate test: N tasks but 1 contract
  FAILS design→build. Dogfood an SDD design phase and assert N NN-prefixed contracts each ≤2 tags.

### H. Human-in-the-loop approval gate  *(finding #8)*

- **Problem:** No human checkpoint; a forward transition fires the instant deterministic exit gates
  pass, on both the proxy auto-transition (`proxy_signal.py:524-550`) and the CLI
  (`phase.py run_phase_set`), and `--force` bypasses all gates.
- **Root cause:** Exit gates are purely artifact-shape predicates; nothing represents "a human said
  go." The two forward-mutation sites are `proxy_signal.py:540` (`_write_phase_atomic`) and
  `run_phase_set`; `watch.py` is read-side only.
- **Recommendation:** One deterministic leaf predicate + a CLI + a force carve-out. (a) New
  `approval_recorded` predicate (embed-free): NOT_MET unless `.agentalloy/approved/<phase>` exists
  **and** post-dates the exit artifact (`since` glob → staleness via mtime/hash). Wire into the
  spec and design `all_of` so should_transition=False blocks **both** mutation sites. Use arg name
  `since` (not `path`) so the prefilter emits no misleading advisory; add an "awaiting approval"
  advisory branch. (b) New `agentalloy approve <phase>` subcommand: atomically write the marker with
  `{approver, approved_at, artifact_sha256}`, then call `run_phase_set(next)` (auto-advance, so it
  doesn't depend on the timing-out reranker re-firing). (c) **Close the `--force` hole** — an
  unconditional pre-force approval check for forward routes from {spec,design}; `--force` then bypasses
  only artifact-completeness, never the human gate. (d) Workflow prose: replace "advance yourself…
  `phase set design`" with "PRESENT in full and STOP; run `approve` only if the user explicitly
  approves"; add `agentalloy approve {spec,design}` to `prose_invariants`. Per-route: gate ON at
  spec→design and design→build; sdd-fast behind `SDD_FAST_REQUIRE_APPROVAL` (default OFF).
- **Effort:** M · **Risk:** medium
- **Files:** `signals/predicates.py`, `signals/gates.py`, `install/subcommands/phase.py`,
  `install/subcommands/approve.py` (new), `install/__main__.py`,
  `_packs/sdd/{sdd-spec-and-scoping,sdd-design-and-planning,sdd-fast}.yaml`
- **Test strategy:** Unit: predicate NOT_MET/MET/stale; `decide_transition` false until marker. CLI:
  `run_phase_set('design')` blocked **even force=True**; `approve` writes + advances. Hermetic e2e:
  completion-intent turn unapproved → phase unchanged + advisory; marker → transition. Invariants:
  prose override dropping `approve` rejected. Config-consistency: predicate registered in spec+design
  packaged gates.

---

## 3. Dependency-Ordered Sequencing

| # | Area | Why this slot | Depends on |
|---|------|---------------|------------|
| 1 | **LOG_LEVEL + rerank INFO line** (A) | Instrument. Every other todo is being root-caused with app INFO/DEBUG suppressed; cheap, isolated, entrypoint-independent. | — |
| 2 | **Passthrough telemetry write** (B) | Measurement. Until traces persist, Stage B / retrieval quality cannot be measured over time; also makes the Stage B `timeout` outcome observable in the store. | — (pairs with #1) |
| 3 | **Stage B latency pool-cap** (C) | Prerequisite for Stage B ever reaching the HIT path. One-line, low-risk — but do **not** ship to prod alone (see #4 / cross-cutting risk). | #1, #2 to observe/measure |
| 4 | **Stage B selection + observability** (D) | Must land **with** #3: a HIT today disables diversity selection at an inert threshold → regression. Recalibrate threshold, sort/refill, fix `/health` + breaker. | #3 (reach HIT), #1/#2 |
| 5 | **Retrieval budget + fusion + tag-filter** (E) | The actual symptom fix for the thin/wrong build contract; independent of Stage B and higher-leverage (Stage B can't inject fragments never in the pool). | #1/#2 to measure; corpus-hygiene decision |
| 6 | **Corpus authoring + poison-tag strip** (F) | Adds the 2–3 missing skills and repairs mis-tagged backend packs; benefits from #5's filter landing. | re-embed pipeline; #5 |
| 7 | **sdd-design density prose + gate** (G) | Fixes density via granularity at k=2; coordinate with #5's k decision. Pack-gated. | pack re-embed; coordinate w/ #5 |
| 8 | **Human approval gate** (H) | Fully independent deterministic feature; can run in parallel with any of the above. | — |

Tracks #5–#7 share a single corpus re-embed / image rebuild — batch them. Track #8 can start day one.

---

## 4. Branch / PR Groupings

- **PR-1 `obs/log-level`** — finding #7 (+ the companion rerank INFO log line). *Why together:* pure
  observability, no behavior change; the log line is the thing the level fix surfaces. Ships first.
- **PR-2 `telemetry/passthrough-trace`** — finding #6. *Why alone:* isolated to
  `proxy_passthrough_router.py` + its test; mechanical; lands right after PR-1 to complete the
  instrument layer.
- **PR-3 `stage-b/viability`** — findings #1 + #2. *Why together:* same subsystem
  (`lm_assist.py`/`domain.py`/`rerank.py`); #2's A/B requires #1's fast reranker to HIT, and shipping
  #1 alone risks a HIT-path quality regression. The latency one-liner may be cherry-picked as an
  urgent hotfix **only if** `LM_ASSIST` stays gated until #2 lands.
- **PR-4 `retrieval/budget-fusion`** — findings #3 (engine) + #4. *Why together:* both target the k=2
  cap, polluted pool, and `contract_tags`-as-steer; the small-k selection gate and soft domain filter
  are interdependent. The biggest symptom fix.
- **PR-5 `corpus/frontend-skills`** — finding #3 (content). *Why separate:* content not code,
  re-embed/image-rebuild gated; authoring `vite`/`calendar`/`vitest` + stripping poison tags.
- **PR-6 `sdd/build-contract-density`** — finding #5. *Why separate:* sdd-*.yaml prose + templates +
  design exit-gate; SkillVersion-bump + re-embed gated; independent of Stage B.
- **PR-7 `sdd/approval-gate`** — finding #8. *Why separate:* deterministic phase-machine feature
  touching none of the other subsystems; can ship anytime.

Batch the re-embed for PR-5 + PR-6 (and any pack edits from PR-7) into one corpus rebuild.

---

## 5. Quick Wins vs Deep Threads

**Quick wins (land this week):**
- **LOG_LEVEL fix** (A) — S, one helper, entrypoint-independent. Unblocks everything.
- **Stage B pool-cap** (C) — S, one-line, probe-proven 1506→785 ms. *Caveat:* don't enable
  `LM_ASSIST` in prod until D lands.
- **Passthrough telemetry write** (B) — M but mechanical and isolated; high value (turns the lights on
  for measurement).

**Deep threads (need investigation / labeled data / re-embed / design-intent confirmation):**
- **Stage B selection + threshold calibration** (D) — needs the Qwen3-Reranker P(yes) distribution over
  real fragments, a labeled relevance set, and a design-intent decision (is fusion-order-keep
  intended?), plus an on-vs-off A/B campaign.
- **Retrieval budget + fusion + corpus hygiene** (E) — needs the benchmark-pack-in-production decision,
  re-running the "category gate is performance-neutral" A/B on the contaminated index, and tuning the
  small-k score gate.
- **Corpus authoring** (F) — needs authoritative llms.txt sources (esp. Vite) and a re-embed cycle.
- **Approval gate** (H) — needs a threat-model decision (cooperative-agent trust vs hard
  unforgeability) and the sdd-fast default.

---

## 6. Cross-Cutting Risks

1. **Live-env drift must be reconciled before any Stage B / retrieval measurement.** Two listeners
   exist — a container `0.0.0.0:47950` with `LM_ASSIST=off` and the native tool `127.0.0.1:47950` with
   `LM_ASSIST=arbitrate` (localhost served by the native one). Measuring against the wrong listener
   yields false conclusions. Decide whether the container instance is retired and reconcile the
   config-vs-process env.
2. **Shipping the Stage B latency fix alone can regress quality.** Once it HITs, the current HIT path
   bypasses `skill_granular_select` and filters at an inert 0.05 threshold in fusion order → diversity
   selection is turned off for no benefit. Gate `LM_ASSIST` until selection logic (D) lands, or ship C+D
   together.
3. **Pack edits propagate only on a SkillVersion bump + corpus re-embed + container image rebuild**
   (proxy is the image, not the source tree). PR-5/6 (and any PR-7 yaml) all hit this — batch one
   re-embed. The runtime gate reads packaged YAML directly (`exit_gates_for_phase`), so `phase set`
   enforces new gates DB-free immediately, but the **proxy's composed prose** needs the re-embed.
4. **The "category gate is performance-neutral" A/B predates benchmark-pack contamination** — its
   conclusion may not hold for the live polluted index. Re-validate before relying on phase-agnostic
   retrieval, and decide whether snowflake/data-engineering/vue/temporal/fastapi belong in the
   production corpus at all (memory says external packs are benchmark-only — they leak today).
5. **Any k increase must move `DEFAULT_MAX_TOKENS_BY_PHASE` in lockstep** to avoid the token-bloat /
   T8-ramble truncation the k=2 default was chosen to prevent.
6. **Telemetry will suddenly start recording `timeout` outcomes** once PR-2 lands — expected, but
   dashboards/alerts should anticipate the spike (it is real, not a new bug).
7. **Config-consistency / drift guards must enumerate every new knob** introduced here:
   `LM_ASSIST_KEEP_THRESHOLD` across presets, pool-width/`n_parallel` coupling, the `approval_recorded`
   predicate in spec+design packaged gates, and the sdd-density prose tokens — or they silently drift.
8. **Re-embed locks the DuckDB corpus held by the running service.** Coordinate corpus changes
   (PR-5/6) with a service restart; concurrent SDD phase state is per-repo and contended (don't
   `phase set` to "fix" a phase another session owns).

---

## 7. Investigate Further (left open by the findings)

- **Is build k=2 a deliberate token-savings policy or an unrevisited default?** (asked by E, F, G) — if
  deliberate, retrieval-quality (fusion + soft filter + selection gate), not budget, is the only lever,
  and the sdd density prose becomes the primary fix.
- **Should benchmark packs (snowflake/data-engineering/vue/temporal/fastapi) be in the production corpus
  at all,** or only a separate benchmark index? They out-rank React in live retrieval today.
- **What is the actual P(yes) distribution** of Qwen3-Reranker-0.6B over real fragment documents at
  :47952? Needed to pick a defensible `keep_threshold` (the 0.45 classifier value used a different
  instruct/document framing).
- **Is fusion-order-keep the intended Stage B design** (pure keep/drop arbiter) per the docstring, or
  is the missing re-order the bug? Confirm before changing ordering vs threshold.
- **Was Stage B ever validated end-to-end with a HIT** showing it helps on this corpus, or has it only
  ever run in the timeout/fail-open state since `LM_ASSIST=arbitrate` was enabled?
- **Does the Vulkan llama.cpp build do true continuous batching** across the 4 slots, or serialize per
  wave? The staggered launch gaps suggest partial serialization — if so, more slots won't give linear
  speedup, further favoring the client-cap fix over `--parallel 12`. Also: why did llama.cpp auto-pick
  `n_parallel=4` (memory-fit heuristic vs build default), and would `--parallel 12` even be granted on
  the 3060?
- **Should `_MAX_CANDIDATES` be coupled to `n_parallel` via one config knob** so the two constants never
  drift again? (the root cause here was two independently-chosen constants).
- **Should the two reranker consumers** (compose Stage B + intent classifier) share one bounded queue
  against the single GPU, or stay independent with separate latches?
- **Is Stage A intentionally off** in the live env (`RUNTIME_RERANK_*` unset, `reranked=False`) so Stage
  B scores the raw fused pool, or a separate misconfiguration?
- **Should a 7-tag build contract be split into per-work-item Tier-2 composes** (narrower tag sets per
  item) rather than one 7-tag compose? This overlaps the sdd density prose fix (G) — confirm which is
  the intended design lever.
- **Error-path telemetry parity** on the passthrough surface (PR-2 is 2xx-only by design) — is ERROR-row
  parity with the OpenAI surface in scope or acceptably deferred?
- **Approval-gate threat model** — hard unforgeability (marker outside the repo tree) vs
  `artifact_sha256` + telemetry-detectability + `--force` carve-out for parity with the existing
  cooperative-agent trust model? And the **sdd-fast default** (`SDD_FAST_REQUIRE_APPROVAL` ON vs OFF).
- **Is `agentalloy task next` validated for a multi-contract build worklist** (8 contracts), or only
  ever exercised against the single-contract monolith?

---

## 8. Fragment Atomicity — the single-topic guarantee  *(added post-synthesis, owner ask)*

The corpus size distribution (p50=149, p90=306, **p99=433, max=1,301 tok**) is itself a finding. The
product thesis is **single-topic fragments** that can be pointed to at exactly the right moment — so a
1,301-token fragment (≈9× the median) is not "a long fragment," it is **under-sliced**: it bundles
multiple topics, will spuriously match many queries, dilutes BM25 multi-tag fusion, and **corrupts any
retrieval-quality measurement taken against it**. The Stage B doc-cap (§C) is a runtime *symptom* guard;
fragment atomicity is the *root*.

- **Audit (read-only, do FIRST).** Measure fragment size per pack; flag every fragment over a
  single-topic budget (start at **>~400 tok / ~2× p90**) and classify each as genuinely multi-topic
  (must split) vs. long-but-coherent (acceptable). The data says this is a **tail problem, not
  pervasive** — p90 is already a healthy 306 tok, so only the top few percent are suspect. Scope the
  re-slice to that tail; do not boil the ocean.
- **Re-slice the offenders** into atomic single-topic fragments (new fragment IDs) → corpus-authoring
  change requiring a **SkillVersion bump + re-embed + image rebuild**. **Batch it with the §E/§F
  re-embed** (poison-tag strip + benchmark-pack decision) so the corpus rebuilds **once**.
- **Standing lint (prevents regression).** Add an authoring/CI guard that fails when a fragment exceeds
  the single-topic token budget — atomicity becomes enforced, not aspirational; mirrors the existing
  config-consistency guards.

**Sequencing — do this BEFORE the K=2→4 test.** Raising K and measuring retrieval quality against a
corpus with mis-sliced fragments measures *noise*: a fat multi-topic fragment can satisfy a query for
the wrong reason, so you cannot tell whether a higher K improved coverage or merely surfaced a bundle.
Clean the tail, re-embed once, **then** run the K sweep against a corpus whose fragments mean what they
claim. The doc-cap (§C) stays regardless as a runtime floor; on a properly-sliced corpus it should
essentially never trigger.

**Open question:** the right single-topic token budget *is* the authoring contract. p50=149 / p90=306
suggests a natural ceiling around **~350–400 tok** — pin it, then the lint, the §C doc-cap, and the
re-slice target all key off the **same** number. *(Resolved in §9 D1.)*

---

## 9. Locked Decisions  *(owner-approved — these supersede the open questions above)*

| # | Decision | Value | Status | Feeds |
|---|----------|-------|--------|-------|
| **D1** | Single-topic token budget | **400 tok** (authoring / lint / reslice target); **doc-cap = 600 tok** as a *runtime floor a notch above* it | **Locked** (audit may nudge budget to ~450 if the p90–p99 band is genuinely coherent) | #15, #9 |
| **D2** | Benchmark packs in prod | **Remove from the production index; keep a separate benchmark-only index.** Strip confirmed benchmark-only packs (snowflake / data-engineering / temporal / fastapi); **keep Vue only if `#14` confirms it's a genuine product skill** | **Locked** (final pack list from #14 classification) | #14, #13 |
| **D3** | Build/ship retrieval `k` | **4**, exposed as a config knob; **sweep 3–6 against the *resliced* corpus**, moving `DEFAULT_MAX_TOKENS_BY_PHASE` in lockstep | **Provisional value** — final after #15 reslice + sweep | #13 |
| **D4** | Stage B doc-cap | **600 tok** (covers p99, only knifes true outliers; lower mis-score risk than 400). Sits above D1's 400 authoring budget as a non-interfering floor | **Locked** | #9 |
| **D5** | Reranker server slots | **`--parallel 8 -c 8192`** (half KV, 2 waves); **measure throughput-vs-slots on the Vulkan build before considering 12** | **Locked default** — verify on hardware | #9 |
| **D6** | Stage B `keep_threshold` | **Ship gated-OFF until measured.** Set from the reranker's actual P(yes) distribution over real fragments; **~0.4 provisional placeholder only** | **Measure-then-set** (do NOT guess a prod value) | #9 |
| **D7** | Approval-gate threat model | **Cooperative-trust + `artifact_sha256` + telemetry-detectability + `--force` carve-out.** NOT hard unforgeability (over-engineering for a "make the human look" gate) | **Locked** | #10 |
| **D8** | `SDD_FAST_REQUIRE_APPROVAL` | **OFF by default.** Gate ON for spec→design and design→build on the **full** lane only | **Locked** | #10 |

**Two decisions are deliberately data-gated and must not be hard-coded blind:** **D3** (final `k` — empirical sweep against the resliced corpus) and **D6** (`keep_threshold` — from the measured P(yes) distribution). Both ship as a knob / gated-off respectively, so **neither blocks the code batch from starting.** D4/D5 ship as concrete defaults; D1/D2/D7/D8 are firm. The `#15` audit + `#14` classification (landing shortly) only sharpen D1's exact number and D2's pack list.
