# Local Agent Mode (v2) — Task Plan

Status: plan phase — pending build approval
Inputs: `docs/local-agent-design.md` (v2.0, 2026-09-02); spike gate results
(classification 0.909, argument_fill 1.000, loop_two_step 1.000,
hallucination 0.045 — all pass).

## Resolved open questions (2026-09-02)

- **Port:** dedicated **47955** (`agentalloy-local-agent` systemd/launchd unit),
  not the embed port. `LOCAL_AGENT_URL` override covers the dev machine, which
  reuses the existing podman DSpark service on :50001 instead of a second
  llama-server.
- **DSpark:** the pinned prebuilt `llama-server` (b9631) does **not** expose
  `draft-dspark` in `--spec-type` (verified live). The v2.0 installer ships the
  2.6B as a plain server; DSpark is a documented follow-up (bump the pin when a
  prebuilt with DSpark ships, or allow a user-provided binary path).
- **Preset flip:** as the design doc defaults — everything ships **opt-in**
  (`LOCAL_AGENT` off in every preset); GPU presets (nvidia, then radeon per the
  dogfood order) flip on only after **two clean eval runs** (M4 gate).

## Known constraints carried from the spike

- The spike harness scripts are lost (context compaction); the 62-task set is
  regenerated from live repo entities in M2. Prompt wording and the gate table
  are preserved in the design doc + spike memory.
- The 2.6B needs a ≥ 2048-token generation headroom on reasoning-heavy prompts
  (fixed in `protocol.py` builders, temp 0 in code).

---

## M1 — Core module (hermetic; no model required to build)

New package `src/agentalloy/local_agent/` — mirrors the retrieval/
subpackage pattern (protocol, client, validate, loop, router).

| # | Task | Detail |
|---|------|--------|
| 1 | `config.py` | env config: `LOCAL_AGENT`, `LOCAL_AGENT_URL`, `LOCAL_AGENT_MODEL`, `LOCAL_AGENT_TIMEOUT_MS` (30000), `LOCAL_AGENT_MAX_STEPS` (2), `LOCAL_AGENT_MAX_TOKENS` (2048), `LOCAL_AGENT_RESULT_CAP_CHARS` (8000). Cached dataclass + test-reset, following `lm_assist.load_config`. |
| 2 | `protocol.py` | 10-way `Action` enum; per-action `json_schema` response formats ported from `mcp_server._QUERY_ACTIONS` arg shapes + spike fill hints (e.g. `code_symbol.symbol: str — symbol or FQN, no path prefix`; `artifacts.name: str — exact slug (no .md)`; `contracts.slug: str — artifact slug like 'spec'`). Prompt builders for classify/fill/answer with the three spike prompt fixes: 2048-token headroom note, "when to pick `none`" section, fix→build routing, artifact `name`=slug not query, numeric `k`. |
| 3 | `client.py` | thin wrapper over `lm_client.OpenAICompatClient`: stage chat with `response_format` json_schema; plain-text retry on the documented json_schema failure signatures (content-filter `{"error":…}`, `content: null`); error mapping — `LMUnavailable`/`LMTimeout` → 503 `local_agent_unavailable`, `LMModelNotLoaded` → 503 with loaded-models list (prevents the model-not-loaded → 500 hole). Failure latch with escalation cooldown — reuse the rerank `_FailureLatch` by promoting it to a shared module (or a small local copy; decide at build, prefer the shared move). |
| 4 | `validate.py` | index-grounded validation before execution: `code_*` → FQN/symbol resolves via code-index state; `contracts` → slug exists in active pack; `artifacts` → (phase, slug) and `name` exist; `k` clamped. Error strings in the spike's `expected 'x'` shape so one retry can self-correct (same error twice → loop ends). |
| 5 | `executors.py` | in-process execution of the 10 actions — **no HTTP hop**: code-index actions via `get_code_index_state()` (`semantic_search`, `symbol_lookup`, `structural_search`, `related_code`, `knowledge_entities`); `contracts`/`artifacts` via state store; `telemetry` via telemetry store; `get_skill_for` via the compose service function (the same one `compose_router` calls — compose's own fail-open bounds this step). Empty result → "No results for …" digest, never an error. A per-request `ExecutionContext` (code-index state, state store, telemetry store, compose service) is assembled by the router from `app.state`. |
| 6 | `loop.py` | pure state machine, model client injected: transcript `[{step, action, args, result, duration_ms}]`; `classify → none | fill → validate (1 retry) → execute → dedupe`; result caps applied **before** the next classify; step budget (default 2, cap 3 — "max 2 lookups, 1 retry"); named termination (`answered`/`none`/`max_steps`/`error`); answer via plain-text stage; deterministic fallback list on answer failure with `degraded="answer_gen"`. |
| 7 | `telemetry.py` | `local_agent_traces` table in `telemetry.duck` (question hash, model tag, per-step JSON, outcome, degraded, total ms, tokens) — DDL added to `telemetry_store._SCHEMA_DDL` (one canonical CREATE, no per-open churn); `record_local_agent_trace()`; written inline before the response returns, failures never propagate (writer pattern). |
| 8 | `router.py` | `POST /local-agent/ask` `{question, repo_root?, phase?}` → `{answer, steps[], degraded, degrade_reason?, model_tag, total_ms}`; 503 shapes for unreachable/timeout/not-loaded/malformed (fail-open: the agent falls back to manual tool use). |
| 9 | `app.py` + health | mount behind `LOCAL_AGENT=on` (module convention; off → 404, nothing imported — "no code in default installs"); `/health` dependency `local_agent` (`ok | unavailable | not_configured`) + a `LocalAgentConfigView` (URL/model/timeouts) in the `LMAssistConfigView` pattern. |
| 10 | tests | loop state machine with a fake client (all 4 terminations, retry-once, dedupe, caps, degraded answer path); `validate` against fixture state; router fail-open (endpoint dead, model not loaded, malformed output); config parsing. |

**M1 gate:** full pytest green; ruff + pyright clean; diff audit shows only
additive changes (new package + the three seams above) — no existing surface
touched.

## M2 — CLI + eval harness

| # | Task | Detail |
|---|------|--------|
| 11 | `agentalloy ask` | new CLI subcommand (thin HTTP client to `/local-agent/ask`; `--repo`, `--phase`); registered in `__main__` + subcommands. |
| 12 | `eval/local_agent/tasks.py` | regenerate the 62-task set **from live repo entities** so ground truth can't rot: verified FQNs from the code-index symbol graph, real contract slugs from `_packs/sdd/`, real artifact names. Categories: the 8 lookup actions + `get_skill_for` + 8 negative (DO-verb/abstain) cases. Script-generated, committed. |
| 13 | `eval/local_agent/harness.py` | port of the spike harness (classify → fill → validate → loop steps), scoring the 4 legacy gates plus 2 new: **answer faithfulness** (judge: no claim contradicts the transcript) and **degradation correctness** (forced failures produce the reason + partial results, no fabricated entities). |
| 14 | baselines + comparator | `eval/local_agent/baselines.json` (spike numbers are the initial baseline); comparator in the `check_corpus_regression` pattern; run JSONs under `eval/runs/`. |
| 15 | CI wiring | hermetic unit tests ride the existing PR pipeline (M1). The model-dependent harness is a **documented manual gate on the dogfood machine** — 2.6B on the 2-core GitHub runner is too slow for a job; note in the eval README. |

**M2 gate:** `agentalloy ask` works against a stub/real service; harness runs
the regenerated task set and emits baseline JSON.

## M3 — Provisioning (installer)

| # | Task | Detail |
|---|------|--------|
| 16 | `runtime_artifacts.py` | `LOCAL_AGENT_PORT = 47955` in `RUNTIME_PORTS` + `_PORT_UNIT` (`agentalloy-local-agent.service` / `ai.agentalloy.local-agent`). Existing port-reclamation logic then covers it automatically. |
| 17 | `pull_models` | add `LFM2.5-2.6B-Q8_0.gguf` to `_GGUF_URL_MAP` (+ the DSpark F16 draft file when the pinned build supports it); recommend-models plumbing for an optional `local_agent` model role. |
| 18 | `enable_service` | `_render_local_agent_unit` (systemd + launchd): plain `llama-server -m …/LFM2.5-2.6B-Q8_0.gguf -ngl <per target> --port 47955`, ctx sized for 2048 gen + transcript (16384). Best-effort skip when llama-server is missing. **No** `--spec-type draft-dspark` (pinned build lacks it — see risks). |
| 19 | presets | `nvidia.yaml` + `radeon.yaml` get the `LOCAL_AGENT` env block, **off at initial ship** (flip is the M4 gate); `cpu.yaml`/container stay off with a doc line. `write-env` passes the block through (already env-file-driven). |
| 20 | status surface | `agentalloy doctor`/status: local-agent model reachability line (small, optional). |

**M3 gate:** clean-install dry run renders the unit + `.env` block; `write-env
--preset nvidia` shows `LOCAL_AGENT=off` at ship.

## M4 — Verify + ship gate

| # | Task | Detail |
|---|------|--------|
| 21 | live smoke (this machine) | `LOCAL_AGENT=on`, `LOCAL_AGENT_URL=http://127.0.0.1:50001` (existing podman DSpark service), `agentalloy ask` end-to-end; verify determinism (byte-identical decisions across two runs), telemetry rows, `/health` view. |
| 22 | two clean eval runs | all 4 legacy gates + 2 new pass twice in a row → **then** flip presets (radeon first — dogfood machine — then nvidia). |
| 23 | close-out | design doc status → implemented; RELEASE notes; memory updates. |

---

## Out of scope (v2.0, per design doc)

SSE streaming; exposing the router as an MCP server; multi-repo in one
request; write actions; shipping on by default before the eval gate.

## Build order & dependencies

M1 (1–10) → M2 (11–15) → M3 (16–20) → M4 (21–23). M2's harness can be built
against a stub client in parallel with M3's installer work.

## Risks & watch items

- **Answer faithfulness is the only unvalidated quality dimension** — it is the
  M4 gate, not a build assumption.
- **Prompt non-monotonicity:** any prompt change re-runs the eval harness; the
  baseline comparator makes regressions loud.
- **Dev-machine port:** 47955 is for clean installs; this machine deliberately
  points at :50001 via `LOCAL_AGENT_URL` (one extra llama-server on the 3060
  is the cost of not reusing it).
- **DSpark gap:** v2.0 ships the plain 2.6B server; decode is slower than the
  spike's 151 tok/s. Follow-up: bump the llama.cpp pin / user binary path when
  a DSpark-capable prebuilt exists.
