# 00 — MASTER BATCH PLAN (same-day batched execution)

Owner is coding all 8 items today. Two batches:

- **CODE BATCH** (no re-embed): **#8, #11, #13, #9, #10** — land as commits in the order below.
- **CORPUS BATCH** (ONE SkillVersion bump + ONE re-embed + ONE image rebuild): **#15-B, #14, #12** (+ #10's prose rider).

`#8` (logging) is the instrument and lands first. `#13` reshapes the retrieval pool and the
`skill_granular_select` signature, so it lands **before** `#9` (Stage B can only filter fragments
that are in the pool, and `#9` calls the new signature). The corpus batch re-embeds once; `#13`'s
K/deepen-band sweep runs **after** that re-embed because the offender reslice (#15-B) must clean the
fat fragments first or the sweep measures noise.

---

## 1. CODE BATCH — conflict-safe commit order

| # | Item | Lands | Why this slot |
|---|------|-------|---------------|
| 1 | **#8** LOG_LEVEL + Stage B verdict line | **first** | The instrument. Additive `logger.info` verdict at `domain.py:597`; makes the timeout→hit flip observable for #13/#9. No upstream dep. |
| 2 | **#11** passthrough telemetry | early, parallel-safe | Zero file overlap with anyone. Makes `composition_traces`/`/telemetry/*` non-empty so the Stage B `timeout` outcome is observable (the other half of #9's `depends_on`). |
| 3 | **#13** retrieval engine (§E) | **before #9** | Reshapes the pool (E6 gate, E5 soft tag-filter) and changes `skill_granular_select` to keyword-only `scores_by_id`/`deepen_band`. #9 *calls* that function over its survivors, so the signature must exist first. |
| 4 | **#9** Stage B viability (§C+§D) | **after #13** | Rewrites `_maybe_lm_arbitrate` to filter survivors into `skill_granular_select`. Rebases onto #13's new signature; must preserve #8's verdict log. Needs #8+#11 to observe the flip. |
| 5 | **#10** approval gate (§H) — *enforcement code only* | independent | predicates/gates/phase/approve/config are DB-free and land in the code batch. Its **prose rider** (SDD §5/§6 present-and-STOP rewrite) rides the corpus batch with #12 — same `sdd/pack.yaml` bump. |

### Non-overlapping work that can start immediately
- **#13's non-`domain.py` files** (`compose_models.py`, `retrieve_models.py`, `proxy_apply.py`, `compose.py`) have no conflict and can be written before #8 lands; only #13's `domain.py` edits wait on #8.
- **#11** is fully isolated.
- **#10's enforcement code** is independent of the `domain.py` trio.

---

## 2. FILE-CONFLICT MATRIX (any file touched by 2+ items)

| File | Items | Coordination |
|------|-------|--------------|
| `src/agentalloy/retrieval/domain.py` | **#8, #13, #9** | **3-way hotspot. Land order #8 → #13 → #9.** #8 adds the additive `logger.info` Stage B verdict line at `:597` (HIT + disabled/timeout/error fall-through). #13 changes `skill_granular_select` to keyword-only `(ranked, k, *, scores_by_id, deepen_band)`, rewrites call sites `:602`/`:331`, inserts `_soft_tag_filter` after the ranked build (`:545` main, `:324` fallback), and wires the dormant pool gate at the 5 pool reads (`:491-522`, `:301-313`). #9 then rewrites `_maybe_lm_arbitrate` (`:597-602`, `:802-813`) to FILTER survivors (threshold 0.05→0.30) and feed them into #13's new signature. **#9 is the final owner of the `:560-623` selection block** — it must (a) call `skill_granular_select(survivors, k, scores_by_id=…, deepen_band=…)`, and (b) keep #8's verdict line firing. Per #9 §6: #13 lands its `skill_granular_select` change first, #9 rebases onto it. |
| `src/agentalloy/config.py` | **#8, #10** | Different regions, additive. #8 adds idempotent `configure_logging()`; #10 adds `Settings.sdd_fast_require_approval` (default OFF). #8 lands first; #10 appends the field. No real conflict. |
| `tests/test_config_consistency.py` | **#8, #9, #13** | Append-only assertions (no `--log-level` in unit/plist; preset LM knobs; per-phase K presence). Land in commit order; resolve any merge marker by keeping all three blocks. |
| `src/agentalloy/install/presets/{nvidia,radeon,apple-silicon}.yaml` | **#9** (+ deferred #14/#13-E6) | #9 owns the edits (keep_threshold 0.30, `LM_ASSIST_MAX_CANDIDATES=8`, `LM_ASSIST_DOC_CAP_CHARS`). **`AGENTALLOY_PHASE_GATE` ships `off`** in #9's preset edits; the flip to `on` is DEFERRED until #14's `category: benchmark` recategorization is re-embedded and verified. Coordinate the flip as a one-line follow-up on these files after the corpus batch. |
| `src/agentalloy/api/health_router.py`, `install/subcommands/start_rerank_server.py`, `signals/classifier.py` | **#9** (+ #14 reads) | #9 edits (reranker probe, `--parallel 8 -c 8192`, shared `LM_ASSIST_MAX_CANDIDATES` bound). #14's Item-1 env/port/listener drift work reads the same modules but only adds a `doctor.py` drift-guard — no edit collision. Coordinate so #14 doesn't also touch the launcher cmd. |
| `container/entrypoint.sh` + `install/subcommands/container_runtime.py` | **#8** | Single owner in this batch. #8 edits the generator (`:613`, `:709`) then **regenerates** `entrypoint.sh` byte-identically (drift guard `test_container_edge_cases.py:1485`). Independent of #14's optional `doctor.py` container drift-guard. |
| `src/agentalloy/_packs/sdd/sdd-design-and-planning.yaml` | **#10, #12** | **REAL conflict.** Both rewrite §6 prose **and** append a node to `exit_gates.all_of` (#12: `build_contracts_cover_tasks`; #10: `approval_recorded`). **One branch / one editor** makes both edits to the §6 block and the `all_of` block; do NOT let two branches rewrite `all_of` independently. ONE `sdd/pack.yaml` version bump covers both. |
| `src/agentalloy/signals/predicates.py` | **#10, #12** | Both register a new predicate in the `PREDICATES` dict (`:404`). Additive — append both entries (#10: `approval_recorded`; #12: `build_contracts_cover_tasks`). Same editor/branch. |
| `src/agentalloy/signals/gates.py` | **#10, #12** | Both add an advisory in `evaluate_node` (#10: awaiting-approval; #12: NOT_MET coverage hook). Coordinate the advisory block; same editor/branch. |
| `src/agentalloy/_packs/sdd/pack.yaml` | **#10, #12** | **ONE** version bump `1.0.19 → 1.0.20` covering the whole SDD-pack batch (both prose riders). |
| `_packs/{fastapi,snowflake,temporal}/*.yaml` skill files (e.g. `fastapi-dependency-injection.yaml`, `snowflake-tables-and-clustering.yaml`, `temporal-activity-basics.yaml`, `temporal-schedules-and-timers.yaml`, `temporal-workflow-basics.yaml`) | **#15-B, #14** | Same files, different keys. #15-B reslices `raw_prose` seams + `fragments[]` (renumber sequences); #14 flips per-skill `category: engineering → benchmark`. **Single editor applies both edits per file** (reslice first, then category flip) to avoid a merge conflict on the same YAML. |
| `_packs/{fastapi,snowflake,temporal}/pack.yaml` | **#15-B, #14** | **ONE** version bump per pack covering both the reslice and the recategorization. (Each pack version is bumped exactly once across the whole corpus batch.) |

---

## 3. CORPUS BATCH — one SkillVersion bump + one re-embed + one image rebuild

**Members:** #15-B (reslice the offender tail), #14 (benchmark recat + new vite/calendar-ui/[vitest] packs), #12 (SDD build-contract density prose), **plus #10's prose rider** (SDD §5/§6 present-and-STOP rewrite). All ride **one** rebuild.

### Order within the batch
1. **#15 read-only audit** — already run (45 fragments >400 tok; p50=149/p90=306/p95=347/p99=441/max=1301). Reconfirm the offender list. *(No blockers — start now.)*
2. **#15-B reslice** the ~21 DUMP+MULTI offenders (split at topic seams as contiguous `raw_prose` slices, renumber sequences, dedup the two fastify FST_ERR dumps). Produces atomic fragments. **Must precede the re-embed and #13's K sweep.**
3. **#14 hygiene** — flip `category: engineering → benchmark` on all 51 skill YAMLs of the 5 packs (Option B), author new `vite` + `calendar-ui` (+ optional `vitest`) packs (each fragment ≤~280 words to satisfy the 400-tok lint). **Layer the category flip onto the resliced files** (same editor).
4. **#12 SDD density prose + #10 prose rider** — §3/§6/"Not this" density rewrite + approval present-and-STOP rewrite, folded into ONE `sdd/pack.yaml` bump (`1.0.19 → 1.0.20`).
5. **Bump every touched `pack.yaml` version exactly once** (version-bump guard requires it; pack edits propagate only on a bump).
6. **ONE re-embed** (`agentalloy reembed`). **Coordinate a service restart** — re-embed locks the live DuckDB held by the running uvicorn.
7. **Regression check** — `eval/check_corpus_regression` vs baselines (name=0.901, topic=0.921, gold=7/8); `#14` keeps `gold_hit 18/18` because eval runs gate-OFF (Option B keeps benchmark packs in-corpus).
8. **Rebuild container image** — `container-build.yml:55` cache key (`hashFiles _packs/**`) auto-busts on any pack edit.
9. **Restart the service.**
10. **THEN #13's K/deepen-band sweep** — config-only (`AGENTALLOY_K_<PHASE>`, `AGENTALLOY_DEEPEN_BAND`) against the **clean** corpus. Not a code change.

### Decisions that GATE the corpus batch
- **Single-topic token budget = 400 tok** (the master number; binds #15 lint + reslice target + #14 authoring + #9 §C doc-cap floor).
- **Benchmark exclusion = Option B** (recategorize to `category: benchmark`, keeps `gold_hit 18/18`) with the **shared literal `benchmark`** (#14 corpus side = #13 E6 allowlist/env).
- **Final build/ship K = 4** (drives #12's prose `{K}` reference and #13's test asserts).
- **`AGENTALLOY_PHASE_GATE` stays `off`** through this rebuild; flip `on` in production presets only after the recategorized corpus is verified.

---

## 4. DECISIONS-BEFORE-CODING (answer at the start of coding)

1. **Single-topic token budget = 400 tok** — the one number shared by #15's lint/reslice, #14's new-skill authoring, and #9's §C doc-cap. Commit this first; everything downstream keys off it.
2. **Final build/ship K = 4** (vs 5/6). Drives #13 test asserts, #12 prose, #9 selection depth.
3. **Benchmark exclusion = Option B** + confirm the literal string **`benchmark`** is identical in #14's `category:` and #13's E6 allowlist/env (risk #7 drift).
4. **#9 selection = feed survivors into `skill_granular_select` (Option B)**, keep_threshold start **0.30** (pin via labeled A/B; do NOT copy classifier 0.45).
5. **#9 §C doc-cap value** — recommend **2400 chars (~600 tok)** for the still-fat corpus shipping in the CODE batch *today*; it sits above the 400-tok authoring budget as a never-trigger floor once #15-B reslices. (If owner insists on one number, set cap = 1600 chars / ~400 tok and accept tail truncation pre-reslice.)
6. **#13 default postures**: E4 deepen-band ships **inert (0.0)**; E5 contract_tags soft-filter ships **on** (empty-fallback safe); E6 pool-gate ships **dormant** (`AGENTALLOY_PHASE_GATE=off`), flipped on only after the corpus recat is re-embedded.
7. **#10 approval scope**: sdd-fast default **OFF**; since-globs spec=`docs/spec/*.md`, design=`docs/design/**/*.md` (design docs only, not build contracts); cooperative trust (`artifact_sha256` + `--force` carve-out, not hard unforgeability).
8. **#11 error-path parity deferred** (2xx-only; no ERROR rows on 502/5xx/timeout). Quick confirm.
9. **SDD pack = ONE version bump** (`1.0.19 → 1.0.20`) covering #12 + #10 prose; single editor owns the `sdd-design-and-planning.yaml` `all_of` + §6 block.

---

## 5. READY-NOW (zero blockers — start this minute)

- **#15 read-only audit** — already run; reconfirm the 45-fragment offender list (`eval/audit_fragment_sizes.py`).
- **#15-A lint** — CODE, no re-embed; ships immediately (born-green shrinking allowlist + budget-in-sync guard), catches regressions before the reslice lands.
- **#8** LOG_LEVEL + Stage B verdict line — instrument, lands first, no deps.
- **#11** passthrough telemetry — fully isolated, no file overlap.
- **#10 enforcement code** — predicates/gates/phase/approve/config; DB-free, no deps (its prose rider waits for the corpus batch; coordinate the predicates/gates registry edits with #12).
- **#13 non-`domain.py` files** — `compose_models.py`, `retrieve_models.py`, `proxy_apply.py`, `compose.py` (the `domain.py` edits wait for #8).

---

## 6. RECOMMENDED TASK ORDER (all 8)

| Order | Item | Batch | Note |
|-------|------|-------|------|
| 1 | **#8** | code | Instrument; lands first. domain.py verdict log + logging fix + entrypoint regen. |
| 2 | **#11** | code | Isolated; the telemetry half of Stage B observability. |
| 3 | **#13** | code | Reshapes pool + `skill_granular_select` signature on domain.py; lands before #9. Ships reasoned default K=4 + env knobs (sweep deferred to corpus batch). |
| 4 | **#9** | code | Rebases onto #13's signature; integrates #8's verdict log; needs #8+#11 to observe the flip. |
| 5 | **#10** | code | Enforcement code (prose rider rides corpus batch with #12; coordinate predicates/gates/sdd-yaml). |
| 6 | **#15** | corpus | Audit (done) → 15-B reslice; first into the single re-embed. Must precede #13's K sweep. |
| 7 | **#14** | corpus | Recategorize benchmark + author new packs; layered on #15-B's resliced files. |
| 8 | **#12** | corpus | SDD density prose + #10 prose rider; one `sdd/pack.yaml` bump. → then ONE re-embed + regression check + image rebuild + restart → then #13 K/band sweep. |
