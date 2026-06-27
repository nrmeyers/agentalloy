# Plan #14 — Corpus hygiene (§F + risks #1/#4): env reconcile, benchmark-pack exclusion, poison-prose, new frontend skills

**Source of truth:** `PLAN-OF-ATTACK.md` §F + Cross-Cutting Risks #1 and #4.
**Batch:** **CORPUS** — needs SkillVersion bump + corpus re-embed + container image rebuild.
`needs_reembed: true`. Rides the **one** shared corpus rebuild with **#12** (sdd density prose)
and **#15-B** (fragment reslice). See §6.
**Coordinates with:** **#13** (retrieval engine §E). #13 ships E5 (soft `contract_tags`
domain-filter) and E6 (dormant product-category pool gate keyed on a reserved literal
`category: benchmark`). **#14 supplies the corpus side of E6.** Literal agreed with #13 D4: **`benchmark`**.

> **Owner decisions honored.** Benchmark-pack pollution + poison "tags" are corpus content; the
> fragment-atomicity reslice (#15-B) runs in the **same** re-embed so the corpus rebuilds once. The
> single-topic token budget (~350–400 tok) that #15 pins is the **same** number my new skills are
> authored under (≤~280 words/fragment).

## Locked decisions (per PLAN-OF-ATTACK §9 — these override any divergent value below)

- **D2 / benchmark exclusion = Option B (`category: benchmark`). LOCKED mechanism.** Re-categorize, do not delete (deletion breaks `gold_hit 18/18`). Reserved literal **`benchmark`**, byte-identical with #13's allowlist/env.
- **D2 / Vue posture RESOLVED → benchmark.** §9 left Vue conditional ("keep only if #14 confirms it's a genuine product skill"); this plan's classification finds vue (and fastapi) are not eval-gold and only out-rank React as noise → categorize **all five** packs (fastapi / snowflake / data-engineering / vue / temporal) as benchmark. ✓ matches this plan's Option B.
- **D1 / single-topic authoring budget = 400 tok** — new skills authored at ≤400 tok/fragment, same number as #15's lint ceiling.
- **`AGENTALLOY_PHASE_GATE` stays `off`** through this rebuild; the prod flip to `on` is a follow-up after the recat corpus is re-embedded and verified.

---

## 0. What this investigation corrected vs PLAN-OF-ATTACK §F

Two §F framings are **factually wrong against the code** and the plan below builds on the corrected facts:

1. **There are no poison `domain_tags`.** `frontend` and `calendar` appear **nowhere** as a
   `domain_tags` list item (`grep -rn '^\s*-\s*frontend\s*$'` / `calendar` → 0 hits). They are
   **prose words**. BM25 indexes the **`prose` column only** —
   `_FTS_CREATE_SQL = "PRAGMA create_fts_index('fragment_embeddings','fragment_id','prose')"`
   (`storage/vector_store.py:259`; prose column comment `:60`). So a `frontend`/`calendar`
   contract_tag matches fragment **prose**, not a tag. "Strip the tag" is a no-op; the real levers are
   (a) benchmark-pack removal and (b) #13's E5 soft tag-filter. See §3.

2. **The five benchmark packs split into two risk classes for the eval.** `temporal`, `snowflake`,
   `data-engineering` **are** eval gold packs (`eval/domain_tasks.py` `gold_skills=` for
   domain_5/9/14/15 + dbt/scd/airflow); `vue` and `fastapi` are **not referenced by any eval gold**.
   The benchmark hits the **live** `/compose` (`eval/gold_hit.py:14,35,48`), and the regression baseline
   is `gold_hit: 18/18` (`eval/corpus_baselines.json`). Physically deleting temporal/snowflake/
   data-engineering from the production index therefore **breaks gold_hit**; my recommended mechanism
   avoids that. See §2.

---

## 1. Item (1) — dual `:47950` listener reconcile (risk #1)

### Measured on this host (the measurement env risk #1 says must be trustworthy)
- **One** listener on 47950: native `uvicorn agentalloy.app:app --host 127.0.0.1 --port 47950`,
  PID 2507500, started by **systemd `--user agentalloy.service`** (`ss -ltnp`). Its env:
  `LM_ASSIST=arbitrate`, `LM_ASSIST_TIMEOUT_MS=1500`, `LOG_LEVEL=DEBUG`,
  `RUNTIME_EMBED_BASE_URL=http://localhost:47951`, `SIGNAL_INTENT_RERANK_URL=http://127.0.0.1:47952`.
- `/health`: `lm_assist.mode=arbitrate`, `keep_threshold=0.05`, model `Qwen3-Reranker-0.6B-Q8_0.gguf`,
  `timeout_ms=1500`.
- `podman ps`: only firecrawl-* + `llama-heavy` (:60000). **No agentalloy container.** 47951 = embed
  llama-server (PID 2494350), 47952 = rerank llama-server (PID 2524862). No `0.0.0.0:47950` binding.

### Conclusion
**localhost:47950 is served by the NATIVE arbitrate listener** (`agentalloy.service`). The container
`LM_ASSIST=off` listener described in risk #1 is **absent on this host** — already retired (or only ever
existed in a container deployment, not this native one). **No stray to kill here.** Measurements for
#9/#13 against `127.0.0.1:47950` are trustworthy and reflect `arbitrate` + `keep_threshold=0.05`
(the inert threshold #9/§D will fix).

### Recommendation (mostly assertion + a guard; no corpus content)
- **Canonical localhost owner = native `agentalloy.service`.** This matches the proxy-only migration
  (native passthrough is the live transport). Keep it.
- **Add a drift guard** (small CODE rider, can land in #8/§A's PR or a doctor patch — *not* a corpus
  edit): in `install/subcommands/doctor.py`, fail if BOTH a native systemd/launchd unit **and** a
  container are configured to bind 47950 (read `install-state.json` `deployment` + probe
  `podman ps`). On this host it passes (deployment=native, no container).
- **decision_needed:** is an agentalloy *container* deployment expected anywhere in the fleet? If yes,
  document the single-owner invariant per host and which preset each surface uses; if no, no action.

This item is otherwise **green today** — folded here only because risk #1 gates trusting the numbers.

---

## 2. Item (2) — benchmark-pack exclusion DECISION

### Evidence (live)
- React calendar build compose (`contract_tags=["react","calendar","frontend"]`) → top-2 source_skills =
  **`vue-composition-api-reactivity`, `fastapi-websockets`** (both benchmark packs out-rank React);
  `["react"]` alone → `react-server-components`, **`vue-composition…`** (vue still places). React (179
  fragments) is reachable but loses on prose BM25 overlap.
- **No product pack `depends_on`** any of {snowflake, data-engineering, vue, temporal, fastapi}
  (reverse-dep scan over all 36 `pack.yaml`). All five are `always_install: false`. Removing them
  orphans nothing.
- Seed is baked by `agentalloy install-packs --packs all` in `.github/workflows/container-build.yml:95`
  and `.github/workflows/corpus-nightly.yml:123`; the corpus-seed cache key hashes `src/agentalloy/_packs/**`
  (`container-build.yml:55`) → **any pack edit triggers a full re-embed + image rebuild** (the batch trigger).
- Version-bump guard: editing any `_packs/**` file requires bumping that pack's `version`
  (`pack_validation.check_version_gate`, `pack_validation.py:150,194`; `tests/test_pack_version_bump_guard.py`).

### RECOMMENDED — **Option B: re-categorize, don't delete** (arms #13 E6)
Change `category: engineering` → **`category: benchmark`** on every skill YAML in the five packs, and
bump each pack's `version`. Re-embed. This **arms #13's E6 product-category allowlist**: production
presets set `AGENTALLOY_PHASE_GATE=on` (owned by #13) and E6 drops `category: benchmark` from product
composes, while the packs stay **physically in the corpus** so:
- `gold_hit` (run with the gate **off**, the default) still retrieves temporal/snowflake/data-engineering
  → **baseline `gold_hit: 18/18` unchanged, no eval regression**;
- the change is **reversible** via one env flag, no corpus rebuild to undo.

This is strictly better than physical deletion because the eval and production share one live `/compose`;
a category gate lets the same corpus serve both (gate off for eval, on for prod). #13 already supports
this exact handoff (its §E6 "re-categorizes the five benchmark packs to `category: benchmark` … #14
assigns at the pack level").

**Concrete change set (Option B):**
- `category: benchmark` (from `engineering`) in **all 51 skill YAMLs**:
  - `src/agentalloy/_packs/fastapi/*.yaml` (16), `…/snowflake/*.yaml` (10),
    `…/data-engineering/*.yaml` (10), `…/vue/*.yaml` (10), `…/temporal/*.yaml` (5).
    (`pack.yaml` files have no `category:` field — only skills do; do **not** add one.)
- Bump `version: 1.0.6 → 1.1.0` in the **five** `pack.yaml`.
- **Verify before commit:** no test pins these skills to `category=="engineering"` —
  grep `tests/test_bundled_corpus_integrity.py`, `tests/test_corpus_reduction.py`,
  `tests/test_diagnostics_corpus.py`. `bootstrap.py:_VALID_CATEGORIES` (`bootstrap.py:36`) constrains
  **system** skills only (`bootstrap.py:178` is gated on system skills) — domain skills already use
  `engineering`, which isn't in that set, so `benchmark` won't trip ingest validation. Confirm domain
  ingest path (`ingest._validate`) does not enum-check `category` (it does not — no
  `_VALID_CATEGORIES` reference in `ingest.py`).

### FALLBACK — Option A: physical exclusion (NOT recommended; breaks gold_hit)
Add `benchmark_only: true` to the five `pack.yaml`; make `--packs all` skip them
(`install_packs.py:486` `chosen = list(available)` → filter out `benchmark_only`); add an
`all+benchmark` keyword (or `--include-benchmark`) for the eval. Then:
- `container-build.yml:95` keeps `--packs all` (now excludes benchmark) → clean production seed;
- `corpus-nightly.yml:123` switches to `--packs all+benchmark` → gold corpus intact;
- **and** the gold_hit baseline must either drop to product-only **or** the campaign must install
  benchmark packs first. Higher coordination, irreversible without a rebuild. Files: the five
  `pack.yaml`, `install/subcommands/install_packs.py`, both workflows,
  possibly `eval/corpus_baselines.json`. List provided for completeness; **prefer B.**

### decision_needed (owner)
- **B vs A.** (Recommend B.)
- **vue/fastapi posture.** Both are *legit* popular frameworks a user might dogfood, and neither is an
  eval gold pack. Categorize them `benchmark` alongside the rest (uniform; owner named all five), OR
  keep them product and rely solely on #13 E5 to suppress them on frontend tasks. Recommend: categorize
  all five `benchmark` now; a user who wants Vue/FastAPI installs them and runs with the gate off.

---

## 3. Item (3) — "poison tags" (corrected: poison **prose**)

`frontend` / `calendar` are **not** `domain_tags` (see §0). Where they live and what to do:

| token | files (prose hits) | classification | action |
|---|---|---|---|
| `frontend` + **React/Vue** | `fastapi/fastapi-websockets.yaml:46,235` ("modern framework like **React, Vue.js or Angular**") | gratuitous filler, names React/Vue → poisons React/Vue retrieval | **subsumed**: fastapi → `benchmark` (Option B) or removed (A). Optional surgical strip if fastapi stays product — see below |
| `react` (verb) | `temporal/temporal-message-passing.yaml:34,178` ("**react** to incoming messages") | stemmed false positive | subsumed by temporal → `benchmark`/removed |
| `frontend` (core) | `fastapi/fastapi-cors.yaml:28,44,46,50`; `fastapi-websockets.yaml:3` desc; `fastapi-file-uploads.yaml:110` | **core to topic** (CORS *is* frontend↔backend) | **do not strip** |
| `frontend` (product) | `nextjs/nextjs-data-security-patterns.yaml`, `nodejs/node-built-in-test-runner.yaml`, `documentation/writing-api-docs.yaml`, `ui-design/pack.yaml` | legitimate product prose | **do not strip**; generic-tag poison handled by #13 E5 |
| `calendar` (core) | `temporal/temporal-schedules-and-timers.yaml:3,26,177`; `data-engineering/data-engineering-airflow-best-practices.yaml:167,177` | core ("calendar/interval" cron, Gregorian/Chinese calendar timetables) | **do not strip**; subsumed by Option B/A |

**Net:** under Option B/A the React/Vue/calendar poison **disappears for free** (every poisoning
fragment is in a benchmark pack). **No standalone corpus prose edit is required.** The residual generic
`frontend`/`calendar`-as-contract_tag poison is #13 E5's job, not a corpus change.

**OPTIONAL surgical strip** (include only if the owner keeps fastapi as a *product* pack, i.e. rejects
B/A for fastapi): in `src/agentalloy/_packs/fastapi/fastapi-websockets.yaml`, delete the two filler
sentences in the fragments whose `content` covers lines 46 & 235 — "*In your production system, you
probably have a frontend created with a modern framework like React, Vue.js or Angular. And to
communicate using WebSockets with your backend you would probably use your frontend's utilities.*" —
and the matching `raw_prose` lines (the lint requires `fragment.content` to remain a contiguous slice of
`raw_prose`, `ingest._lint`), replacing with a neutral "A browser or service client opens the WebSocket
connection." Bump `fastapi` version. This is the **only** defensible standalone prose strip and it is
conditional.

---

## 4. Item (4) — AUTHOR missing skills (the real corpus add)

### Authoring constraints (from `ingest._validate` / `ingest._lint`)
- Per skill YAML: `skill_id`, `canonical_name`, `description`, `category: engineering` (**product**, not
  benchmark), `skill_class: domain`, `domain_tags` (≤ tier `soft_ceiling`: framework/domain/store=10,
  tooling=8 — keep ≤6), `always_apply: false`, `phase_scope: [build]`, `category_scope: [framework]`
  (vite/vitest) or `[domain]` (calendar-ui), `author: navistone`,
  `change_summary: "hand-authored Opus expansion"`, `embed_model: nomic-embed-text-v1.5`,
  `embedding_dim: 768`, `raw_prose` (the canonical body), and `fragments[]`.
- Fragments: `fragment_type ∈ {setup, execution, example, verification, guardrail, rationale}`,
  `sequence` **contiguous from 1**, `content` **a verbatim contiguous slice of `raw_prose`**
  (whitespace-modulo — `ingest._lint` warns otherwise). **Must include ≥1 `execution`, ≥1 `rationale`
  (R8), ≥1 `verification` (R3).** Word floor 5 / ceiling 2000 (hard); 25/800 (warn).
- **Single-topic budget:** to pass #15's incoming ≤~400-tok lint, keep each fragment **≤~280 words
  (~370 tok)**. Aim 6–11 fragments/skill (mirrors the react pack).
- Sourcing per `_packs/meta/sys-r1-tiered-sourcing.md`: prefer official `llms.txt`. **vite** →
  `https://vite.dev/llms.txt` (R1 tier-1); **vitest** → `https://vitest.dev/`; **calendar-ui** →
  MDN (`role="grid"`, `<time>`, date inputs) + the TC39 **Temporal** API + `date-fns` docs (R1 tier-3
  fallback_root). Record source URL/commit in `change_summary` per R1.
- Registration: new dir under `src/agentalloy/_packs/<name>/` with `pack.yaml` (skills[] →
  skill_id/file/fragment_count) + skill YAMLs. `_discover_packs` auto-finds it (`install_packs.py:447`);
  `--packs all` (the seed) includes it. Version starts **`1.0.0`** (no prior install → version guard moot).

### NEW PACK A — `vite` (tier: **tooling**)  → fixes `['vite']` resolving to Vue
`pack.yaml`: `name: vite`, `tier: tooling`, `version: 1.0.0`, `always_install: false`,
`depends_on: [core, engineering, typescript]`. Skills (file = `<skill_id>.yaml`):

| skill_id | canonical_name | domain_tags | fragment outline (type·topic) |
|---|---|---|---|
| `vite-dev-server-and-hmr` | Vite Dev Server & HMR | vite, dev-server, hmr, esbuild | setup·`npm create vite`; execution·dev server + native ESM; example·`import.meta.hot` HMR API; rationale·why no-bundle dev; verification·HMR boundary checklist |
| `vite-config-and-plugins` | Vite Config & Plugins | vite, vite-config, plugins, resolve-alias | setup·`vite.config.ts` shape; execution·`@vitejs/plugin-react`/`-vue`; example·`resolve.alias` + `define`; guardrail·plugin order/`enforce`; rationale·config vs CLI |
| `vite-build-and-code-splitting` | Vite Build & Code-Splitting | vite, rollup, build, code-splitting, chunks | execution·`vite build` (Rollup); example·dynamic `import()` + `manualChunks`; guardrail·vendor-chunk pitfalls; verification·bundle-analysis check; rationale·esbuild-dev/Rollup-prod split |
| `vite-env-and-modes` | Vite Env Vars & Modes | vite, env, import-meta-env, modes | execution·`import.meta.env` + `VITE_` prefix; example·`.env.[mode]` + `--mode`; guardrail·**never** leak secrets to client; verification·env-exposure check |
| `vite-static-assets-and-workers` *(opt)* | Vite Assets & Workers | vite, assets, public-dir, web-worker | execution·`?url`/`?raw`/`?worker` imports; example·`public/` vs imported; guardrail·hashed-asset caching |

### NEW PACK B — `calendar-ui` (tier: **domain**)  → fixes `['calendar']`; **this is the literal product**
`pack.yaml`: `name: calendar-ui`, `tier: domain`, `version: 1.0.0`, `always_install: false`,
`depends_on: [core, engineering]` (examples in React/TS; retrieval keys on tags, not deps).
`domain_tags` across the pack include **`calendar`, `date-grid`, `datepicker`, `month-view`,
`scheduling-ui`** so `['calendar']` resolves here, not temporal/airflow.

| skill_id | canonical_name | domain_tags | fragment outline (type·topic) |
|---|---|---|---|
| `calendar-month-grid-layout` | Calendar Month-Grid Layout | calendar, date-grid, month-view, css-grid | setup·weeks×days model; execution·first-weekday offset + leading/trailing days; example·CSS-grid 7-col; guardrail·off-by-one on month boundaries; verification·"42-cell / 6-week" check; rationale·grid vs fl/absolute |
| `calendar-date-math-and-ranges` | Calendar Date Math & Ranges | calendar, date-math, timezone, dst, date-fns, temporal-api | execution·start/end-of-month, add/sub days; example·TC39 **Temporal** `PlainDate` + `date-fns`; guardrail·**timezone/DST** footguns (no `new Date()` math); verification·DST-boundary test; rationale·why not millisecond arithmetic |
| `calendar-selection-and-interaction` | Date Selection & Interaction | calendar, datepicker, range-select, controlled-state | execution·single/range/multi selection state; example·drag-to-select range; guardrail·controlled vs uncontrolled; verification·range-invariant (start≤end) |
| `calendar-event-rendering-and-overlap` | Event Rendering & Overlap | calendar, events, multi-day, overlap-layout | execution·place events in day cells; example·multi-day span + all-day row; guardrail·overlap/stacking + z-order; verification·no-clip/overflow check |
| `calendar-keyboard-and-a11y` | Calendar Keyboard & A11y | calendar, accessibility, aria-grid, keyboard-nav | execution·`role="grid"` + roving `tabindex`; example·Arrow/PageUp/Home keymap; guardrail·`aria-selected`/`aria-current="date"`; verification·screen-reader/keyboard checklist; rationale·grid semantics over `<table>` |

### NEW PACK C — `vitest` (tier: **tooling**, **optional** per §F)
`pack.yaml`: `name: vitest`, `tier: tooling`, `version: 1.0.0`, `depends_on: [core, engineering, typescript]`.

| skill_id | canonical_name | domain_tags | fragment outline |
|---|---|---|---|
| `vitest-config-and-environment` | Vitest Config & Environment | vitest, vite, jsdom, test-config | setup·`vitest.config`/shared `vite.config`; execution·`environment: jsdom`, `globals`, `setupFiles`; rationale·why it reuses the Vite pipeline; verification·config sanity |
| `vitest-mocking-and-timers` | Vitest Mocking & Timers | vitest, mocking, vi-fn, fake-timers | execution·`vi.mock`/`vi.fn`/`vi.spyOn`; example·module + fake timers; guardrail·`vi.restoreAllMocks` hygiene; verification·no-leak check |
| `vitest-component-testing` | Vitest Component Testing | vitest, testing-library, component-test | execution·`@testing-library` + jsdom render; example·user-event interaction; guardrail·query-by-role over test-id; verification·a11y-query check |
| `vitest-coverage-and-watch` | Vitest Coverage & Watch | vitest, coverage, v8, watch | execution·`--coverage` (v8/istanbul); example·thresholds in config; rationale·watch/`--ui` loop; verification·coverage-floor gate |

---

## 5. Files touched / new / tests

### Edited (Option B — recommended)
- `src/agentalloy/_packs/{fastapi,snowflake,data-engineering,vue,temporal}/pack.yaml` — `version` bump (×5).
- `src/agentalloy/_packs/{fastapi,snowflake,data-engineering,vue,temporal}/*.yaml` — `category: benchmark` (×51 skill files).
- *(conditional, only if fastapi kept product)* `src/agentalloy/_packs/fastapi/fastapi-websockets.yaml` — strip 2 filler sentences (§3).

### Edited (Option A — fallback only)
- five `pack.yaml` (`benchmark_only: true`), `src/agentalloy/install/subcommands/install_packs.py`
  (filter `all`, add `all+benchmark`), `.github/workflows/container-build.yml`,
  `.github/workflows/corpus-nightly.yml`, possibly `eval/corpus_baselines.json`.

### New (Item 4 — both options)
- `src/agentalloy/_packs/vite/pack.yaml` + 4–5 skill YAMLs.
- `src/agentalloy/_packs/calendar-ui/pack.yaml` + 5 skill YAMLs.
- *(optional)* `src/agentalloy/_packs/vitest/pack.yaml` + 4 skill YAMLs.

### Tests
- `tests/test_retrieval_domain.py` (or a new `tests/retrieval/` case): hermetic compose of the React
  calendar build contract asserts **≥1 `react-*` or `calendar-*`** in source_skills and **0** of
  `{snowflake-*, data-engineering-*, vue-*, temporal-*, fastapi-*}` **when `AGENTALLOY_PHASE_GATE=on`**;
  with the gate off, benchmark packs still reachable (proves no eval regression).
- Reachability: `['vite']` → a `vite-*` skill; `['calendar']` → a `calendar-ui` skill; `['vitest']` →
  `vitest-*` (post-authoring).
- `tests/test_bundled_corpus_integrity.py` — extend so `category: benchmark` is a recognized value and
  the five packs carry it; new packs pass integrity (fragment contiguity, required types).
- `tests/test_pack_version_bump_guard.py` — green (the five edited packs bumped).
- `eval/check_corpus_regression` — confirm `gold_hit` stays **18/18** with the gate off (Option B).
- `tests/test_config_consistency.py` — if Option B, assert the `benchmark` literal + `AGENTALLOY_PHASE_GATE`
  posture match #13's E6 (shared-knob drift guard, risk #7).

---

## 6. Sequencing & batch coordination

- **One corpus rebuild** carries #14 (this) + **#12** (sdd-* prose) + **#15-B** (reslice). Order inside
  the rebuild: author new packs → re-categorize benchmark packs → (apply #15-B reslices, #12 prose) →
  `install-packs --packs all` → reembed → stage corpus-seed → image build. The
  `container-build.yml` cache key (`hashFiles('_packs/**')`) auto-busts.
- **#15 BEFORE the #13 K-sweep.** My new skills are authored ≤~280 words/fragment so they are born
  compliant with #15's lint; re-categorization touches no fragment sizes.
- **#13 is CODE and lands independently**, but its E6 gate is **inert until this corpus re-embed assigns
  `category: benchmark`**. Ship #14's re-embed, then #13 flips `AGENTALLOY_PHASE_GATE=on` in the presets.
  If the owner picks Option A instead, #13 E6 stays dormant and harmless (its plan says so explicitly).
- **Literal lock-in:** `benchmark` (string) is shared by #14 (`category:` value) and #13 (E6 allowlist
  exclusion + env). Do not diverge.

## 7. Decisions needed (blocking before coding)
1. **Option B (re-categorize, recommended) vs Option A (physical exclude).** Drives whether this is
   corpus-pure (B) or corpus+install-code (A) and whether gold_hit baseline moves.
2. **vue/fastapi**: categorize `benchmark` like the other three, or keep product? (Recommend benchmark.)
3. **vitest pack**: author now or defer? (§F marks it optional. Recommend author — cheap, fixes `['vitest']`.)
4. **calendar-ui home**: dedicated pack (recommended, so `['calendar']` resolves cleanly) vs fold into
   `ui-design`/`react`.
5. **Container deployment expected in the fleet?** (Item 1 drift-guard scope.)
6. **Confirm `benchmark` literal** with #13 (D4) before either side codes.
