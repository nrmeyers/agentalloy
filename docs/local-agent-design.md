# Local agent mode (v2): a 2.6B model driving the query protocol

Status: **design sketch (2026-09-02, pre-implementation).** Phase: design —
spec/feasibility closed by the `~/dev/newagent` spike (all 4 gates green,
10-way-over-two-gate decided). This document is the design-phase deliverable;
the plan phase decomposes it into tasks.

## Summary

AgentAlloy v1 is a just-in-time context engine: the *harness's* model is the
driver, and the service composes context and answers queries. **Local agent
mode (v2) adds a fully-local, zero-cloud-token read-only intelligence agent:**
a question goes to the service, the local LFM2.5-2.6B model classifies it into
one of 10 actions, fills the arguments under the action's JSON schema, the
service executes the query in-process, the model decides whether to loop or
stop, and finally generates an answer from what was retrieved.

The spike proved the model can drive this protocol deterministically
(temperature 0, schema-constrained): classification 0.909, argument_fill
1.000, loop_two_step 1.000, hallucination 0.045 — all four gates pass. This
document decides **where the loop lives** (a new opt-in module in the FastAPI
service), **how the residual hallucination risk is handled** (deterministic
index-grounded validation + one retry + structured degradation), and **how the
stage fails open** (the service's existing LM-stage conventions).

## Scope fence — what this is not

- **Not a code-editing agent.** All 10 actions are reads. A DO-verb request
  ("fix the auth bug") classifies to `none` or an investigative query; the
  answer says what was found, not that anything was changed.
- **Not a harness model.** The harness keeps its cloud model for coding. This
  is a parallel query surface (`agentalloy ask` / `POST /local-agent/ask`),
  not a `model=` route on the passthrough paths — pointing a coding harness at
  a 2.6B model is out of scope.
- **Not in the compose/proxy path.** Existing traffic is untouched and the
  "deterministic by default" claim for v1 surfaces is unchanged: the module
  ships `off` by default and, when on, adds only new endpoints.
- **No writes.** No state/contract/artifact mutation; the loop cannot advance
  a phase or record anything except its own telemetry trace.

## The evidence (spike record)

Harness: `~/dev/newagent` — classify (10-way enum via strict JSON-schema
`response_format`) → constrained fill (per-action JSON schema) → validate →
one retry per stage → score. 62 tasks / 66 steps, all entities real (verified
FQNs, real contract slugs from `_packs/sdd/`, real artifact names). Target:
LFM2.5-2.6B-Q8_0 + DSpark (llama-compressor) at `127.0.0.1:50001`,
temperature 0 — **decision-level byte-identical across re-runs.**

Gates (final, after two prompt iterations): classification 0.909 ≥ 0.85,
argument_fill 1.000 ≥ 0.95, loop_two_step 1.000 ≥ 0.70, hallucination
0.045 < 0.05. The prompt fixes that earned the green: max_tokens 512→2048
(reasoning exhausted the budget on abstention cases → empty content) plus a
detailed "When to choose `none`" section; fill-stage disambiguation hints
(fix→build, `artifact_body` slug-vs-query, telemetry numeric-k).

Known weak spots carried into the design:

1. **Abstention:** DO-verb + strong codebase-noun requests misfire to
   `code_search` (3/8 negative tasks). Mitigated in-loop: a misfired search
   returns "No results", which lands in the transcript and lets the next
   classify step correct course to `none`.
2. **Thin hallucination margin** (0.045 vs 0.05 cap): schema constraints stop
   structural hallucination (action/field), but *value* hallucination (a
   plausible-looking FQN or slug) is the residual. The design below converts
   this from a measured risk into a deterministic reject-retry path.
3. **Non-monotonic prompt boundaries:** the fill fixes regressed two other
   tasks. Consequence: the eval suite is a hard gate on **any** prompt change,
   in-repo, re-run in CI.

> **Artifact caveat:** `~/dev/newagent` now contains only `README.md` and
> `config.toml` — `protocol.py`, `tasks/tasks.jsonl`, and `results/` are gone.
> The plan phase must (a) port the protocol from this document + spike memory,
> and (b) **regenerate the task set** from live repo entities (it was built
> from verified FQNs / real slugs / real artifact names, so it is
> regenerable by script). Do not treat spike numbers as reproducible until
> the harness is back in the repo.

## Architecture

### Placement: a new module in the FastAPI service

The loop lives in the agentalloy service itself, as an opt-in module —
`src/agentalloy/local_agent/` — not as a sidecar and not inside the MCP
server. Reasons:

- **All 10 actions are local-service reads.** The MCP fallback
  (`install/mcp_server.py`) executes them as HTTP calls against this very
  service. In-process execution drops the network hop and reuses the
  lifespan-scoped handles the service already owns (corpus store, state store,
  telemetry store, `CodeIndexState`, orchestrator). A sidecar would need all
  of those again.
- **The conventions are here.** Config (env-driven, preset-shaped), health
  (dependency status + config view), failure latch with escalating cooldown,
  telemetry (DuckDB), and structured 503s (`LMModelNotLoaded` pattern) all
  exist; a sidecar would re-derive them.
- **Mounting follows the module convention** (`create_app`): the router
  registers only when `LOCAL_AGENT=on`; a disabled module's endpoints 404
  rather than 503, same as `code_index`.

```
src/agentalloy/local_agent/
  config.py     env-driven stage config (LM_ASSIST pattern: os.environ +
                cached dataclass, reset for tests), preset-shaped defaults
  protocol.py   the 10-way surface: action enum, per-action JSON schemas,
                classify/fill/answer prompt builders (ported from spike)
  client.py     thin wrapper over lm_client.OpenAICompatClient (reuse, not
                fork — it already has the error taxonomy, response_format,
                list_models/ensure_model_loaded)
  validate.py   index-grounded argument validation (below)
  loop.py       the classify → fill → validate → execute → (loop|stop)
                state machine; pure, model-client-injected (testable)
  router.py     POST /local-agent/ask (+ health config view)
  telemetry.py  local_agent_trace records
```

### Surface

- **REST:** `POST /local-agent/ask` — body `{question, repo_root?, phase?}`.
  Response: `{answer, steps: [{step, action, args, validation, result_chars,
  stage_latencies}], degraded, degrade_reason?, model_tag, total_ms}`. v2.0 is
  non-streaming JSON; the step trace makes the response inspectable, and SSE
  streaming (per-step progress while the final answer generates) is a noted
  follow-up, not v2.0 scope.
- **CLI:** `agentalloy ask "..." [--repo <path>] [--phase <p>]` — a thin
  client to the endpoint (name verified free in the subcommand table).
- **No MCP surface in v2.0.** An external model already has `agentalloy_query`
  directly; wrapping the local agent in an MCP tool adds a hop without a user.

## The loop

```
ask(question, repo, phase?)
  transcript = [initial context: question, repo scope, phase if resolvable]
  for step in 1..MAX_STEPS:                       # default 2, hard cap 3
    action = classify(transcript)                 # 10-way, json_schema enum
    if action == "none": break                    # stop; answer from transcript
    args   = fill(action, transcript)             # per-action json_schema
    check  = validate(action, args)               # deterministic, index-grounded
    if check fails:
      args = fill(action, transcript + check.error)   # ONE retry (spike pattern)
      if revalidate fails: return degraded(check)     # no fabricated execution
    result = execute(action, args)                # in-process, existing handlers
    if duplicate(action, args, transcript): break # same query twice → stop
    transcript.append(action, args, digest(result)) # result capped at
                                                    # RESULT_CAP_CHARS
  answer = generate(question, transcript)         # final 2.6B completion
  if answer fails/empty: return structured results, degraded="answer_gen"
  return {answer, steps, degraded=False}
```

Design points:

- **`none` has two jobs** and the classify prompt must keep them distinct
  (the spike's "When to choose `none`" section does): (a) *abstention* — the
  request isn't answerable by codebase intelligence (code edits, opinions,
  out-of-domain); the answer then says so and offers what a query *would*
  find; (b) *sufficient context* — prior steps already retrieved enough.
  Both end the loop; the answer stage sees the transcript either way.
- **Transcript discipline is the 2.6B's lifeline.** Every executed result is
  capped at `LOCAL_AGENT_RESULT_CAP_CHARS` (default 2400, mirroring
  `LM_ASSIST_DOC_CAP_CHARS`) before entering the next classify prompt. The
  DSpark compressor sidecar on the serving model handles the rest of the
  latency, not the context budget — the cap is what keeps multi-step requests
  inside the window.
- **Termination is total:** `none`, step budget, duplicate detection, or
  failed validation after retry. There is no path where the loop runs until
  the model "feels done" — every exit is a named, telemetry-able reason.
- **The final answer is the one stage the spike did not validate.** It is a
  plain completion (no schema) over question + transcript at temperature 0.
  Design stance: free generation, with a **deterministic fallback** — if the
  completion errors or comes back empty, the response carries the raw step
  results with `degraded: "answer_gen"` (exactly the v1 fail-open posture:
  degrade to the deterministic artifact, never invent). Answer faithfulness
  gets its own eval gate (below) before the mode may ship on by default.
- **`get_skill_for` maps to `/compose`** as in the spike: fill produces
  `(task, phase)` with the spike's fix→build routing (sdd-fast only for
  explicit quick-fix/hotfix). Compose's own fail-open behavior bounds this
  step.

## The 10-way surface (grounded in `install/mcp_server.py`)

| # | action | args (schema-enforced) | execution |
|---|--------|------------------------|-----------|
| 1 | `code_search` | `query`, `k` | `GET /code/search/semantic` |
| 2 | `symbols` | `query` (FQN) | `GET /code/search/symbol` |
| 3 | `knowledge_why` | `query` (FQN) | `GET /code/search/structural?query=governing_decisions` |
| 4 | `knowledge_related` | `query`, `k` | `GET /code/search/related-decisions` |
| 5 | `knowledge_entities` | `query`, `kind?` | `GET /code/search/entities` |
| 6 | `artifact_body` | `phase`, `slug`, `query` (artifact name) | `GET /state/artifact/{phase}/{slug}/{name}` |
| 7 | `contract_detail` | `slug` (contract ID) | `GET /contracts/{id}` |
| 8 | `telemetry` | `k`, `phase?` | `GET /telemetry/traces` |
| 9 | `get_skill_for` | `task`, `phase` | `POST /compose` |
| 10 | `none` | — | loop terminator |

Schemas port from the spike's `protocol.py` (which in turn mirror the MCP
tool definitions above), so the local agent speaks the same language the
injected guidance teaches the harness model to use. Execution reuses the
existing routers/handlers in-process (the code-index and state stores are
already on `app.state`); no new store access is introduced.

## Validation: index-grounded, deterministic

The spike validated against an offline ground-truth file. In production the
ground truth **is** the running index — so validation becomes a first-class
stage, and the probabilistic hallucination gate becomes a deterministic
reject path:

- `symbols`, `knowledge_why`, `knowledge_entities`: FQN (or short name) must
  resolve in the symbol graph before execution.
- `artifact_body`: `(phase, slug)` contract and named artifact must exist in
  the state store.
- `contract_detail`: contract ID must exist.
- `code_search`, `knowledge_related`, `get_skill_for`: free text — no
  existence check; but an **empty result is a signal**: the digest records
  "No results", so the next classify step can re-query differently or
  abstain. This is the in-loop mitigation for the DO-verb misfire weak spot.
- `telemetry`: `k` clamped to sane bounds.

On failure: the error is fed back in the spike's `expected 'x'` form, one
retry, then the request returns **degraded with the reason and whatever was
already retrieved** — a fabricated FQN never executes, and the caller sees
exactly why it stopped rather than a plausible-wrong answer.

## Failure & degradation paths

Follows the service's existing LM-stage conventions (fail-open, structured
errors, `/health` dependency, escalating cooldown latch):

| Failure | Detection | Behavior |
|---|---|---|
| Module off | toggle | endpoints 404 (module convention) |
| Model endpoint down / 5xx / timeout | `LMUnavailable` / `LMTimeout` | structured 503 `local_agent_unavailable` + reason; failure latch enters escalating cooldown (dead `:50001` is not probed every request — mirrors the rerank latch) |
| Model not loaded | `ensure_model_loaded` → `LMModelNotLoaded` | structured 503 with the loaded-model list (v5.3 directive pattern) |
| `json_schema` unsupported by the build | empty content + `finish_reason=stop` under `response_format` | one plain-text retry of that stage (the documented failure signature in `lm_client.chat`); if it persists → 503 with the build-capability hint |
| Invalid after retry (classify or fill) | schema/enum failure | degraded response: reason + partial steps; never a fabricated action |
| Validation failure after retry | index-grounded check | degraded response: `expected 'x'` detail + partial steps |
| Step budget exhausted | loop counter | **not** degraded — answer from gathered context |
| Answer generation fails/empty | `LMBadResponse` / empty | `degraded: "answer_gen"` + raw step results (deterministic fallback) |
| Downstream stage in an executed query (embed etc.) | existing fail-open paths | unchanged; the degraded result digest flows into the transcript |

`/health` gains a `local_agent` dependency (`ok | unavailable |
not_configured`, impact: "local agent mode unavailable") plus an
`LMAssistConfigView`-style config view (mode, model, url, max_steps, timeouts)
for preflight. The model tag is pinned in config and recorded in every trace
(provenance, like the reranker tag).

## Configuration

Env-driven, bare names, preset-shaped (the `LM_ASSIST*` pattern):

| var | default | notes |
|---|---|---|
| `LOCAL_AGENT` | `off` | `off` \| `on`; unknown values → `off` + one warning |
| `LOCAL_AGENT_URL` | `http://127.0.0.1:50001` | OpenAI-compatible endpoint |
| `LOCAL_AGENT_MODEL` | `lfm2.5-2.6b-compressor` | pinned model tag |
| `LOCAL_AGENT_TIMEOUT_MS` | `30000` (per stage) | the spike's 300 s was a cold-server wall number; warm stages are seconds. The "thinks before answering" reasoning budget is inside `max_tokens`, not the timeout |
| `LOCAL_AGENT_MAX_STEPS` | `2` | hard cap 3 |
| `LOCAL_AGENT_MAX_TOKENS` | `2048` | spike finding: 512 is exhausted by internal reasoning on abstention cases |
| `LOCAL_AGENT_RESULT_CAP_CHARS` | `2400` | per-result transcript cap |

Temperature is fixed at 0 in code (not env): determinism is a property of the
protocol, not a setting. **Determinism story:** temp 0 + pinned model tag +
schema constraints → classify/fill decisions are byte-identical across re-runs
(the spike's measured property); the final answer is temperature-0 generation
(near-deterministic on the same transcript, but the doc claims decision-level
determinism only for the loop, generation-level only for the answer).

## Installer, presets, provisioning

- **New service unit** `agentalloy-local-agent` (systemd) /
  `ai.agentalloy.local-agent.plist` (launchd): `llama-server` serving the
  2.6B GGUF with the DSpark compressor sidecar on `:50001`, written alongside
  the embed (`:48951`) and rerank (`:48952`) units in
  `install/subcommands/enable_service.py`, including the existing port-reclaim
  logic. Best-effort skip + log when `llama-server` is absent, same as the
  other two units. Exact compressor launch args: pinned in the plan phase
  from the working 3060 setup.
- **Model pull:** setup wizard downloads the 2.6B GGUF + compressor draft
  alongside the embed/rerank GGUFs (`pull_models.py`).
- **Hardware split** (the established posture): GPU presets (`nvidia`,
  `radeon`) provision the unit and set `LOCAL_AGENT: "on"`; `cpu.yaml` and
  the container preset ship `off` — interactive multi-stage inference on CPU
  is not inside any latency budget, and Q8 2.6B + compressor wants ~8 GB VRAM.
- **Shared-endpoint note:** `:50001` is also where some deployments run the
  fastModel/compactionModel. If the preset already assigns that port to
  another role, the plan phase decides: dedicated port vs. shared server
  (shared works only if the model tag matches; the cooldown latch protects
  against either being wrong).

## Telemetry

New record type `local_agent_trace` in the existing telemetry DuckDB
(service-owned, same store as composition traces): question hash, per-step
rows (action, args, validation outcome, result chars, stage latencies),
final outcome (`answered | degraded:<reason> | unavailable`), model tag,
total latency, token accounting. Queryable through the existing
`/telemetry` surface (and therefore through the `telemetry` action itself).
This is also the production feed for the eval harness: misfire patterns
(DO-verb → `code_search` → no results → recovered?) become measurable in the
field, not just in the spike suite.

## Evaluation & test plan (QA gate inputs)

1. **In-repo eval harness** (`eval/local_agent/`): the spike's harness
   ported — task set regenerated from live repo entities (verified FQNs, real
   slugs, real artifact names), same four gates (classification ≥ 0.85,
   argument_fill ≥ 0.95, loop_two_step ≥ 0.70, hallucination < 0.05). Runs in
   CI; **any prompt change must re-run the full suite** (non-monotonic
   boundaries).
2. **New gates the spike couldn't measure:**
   - *answer faithfulness* — judged pass: the answer makes no claim
     contradicted by its transcript; judged on the task subset that reaches
     the answer stage.
   - *degradation correctness* — on forced validation failure and forced
     answer-gen failure, the response must carry the reason + partial results
     and must not contain fabricated entities.
3. **Unit tests:** loop state machine with an injected fake model client
   (termination conditions, retry-once semantics, duplicate detection, cap
   enforcement); `validate.py` against fixture index/state; router fail-open
   paths (dead endpoint, malformed response, model-not-loaded).

## Risks

- **Unvalidated answer stage.** Largest open quality risk; gated by the
  faithfulness eval before any preset flips `on` by default (first shipped
  state is opt-in).
- **Thin hallucination margin** (0.045 vs 0.05): index-grounded validation
  converts structural value-hallucination into a deterministic reject, but
  *free-text* query hallucination (a plausible `code_search` phrase) still
  degrades to "No results" rather than a hard error — acceptable, and it is
  what the loop is designed to recover from.
- **Prompt non-monotonicity:** any future prompt edit can silently move other
  decision boundaries; the CI suite is the only guard, so the prompt lives
  in-repo (`local_agent/protocol.py`) and the eval is wired to it.
- **Latency:** multi-stage worst case is minutes on a cold server; DSpark
  keeps the warm path fast but the budget is per-stage, not end-to-end.
  Streaming (follow-up) is the UX answer.
- **Serving dependency:** the mode is only as available as `:50001`; the
  cooldown latch + `/health` dependency make that visibility match the rest
  of the service's posture.

## Open questions (plan phase)

1. Exact `llama-server` + DSpark compressor launch args and model file URLs
   (from the working 3060 setup; not recoverable from the spike dir).
2. `:50001` port policy — dedicated unit vs. shared with an existing
   fastModel/compaction deployment.
3. Default-on decision: which preset(s) flip `LOCAL_AGENT=on` by default, and
   what faithfulness score clears the bar (propose: nvidia first, after two
   clean eval runs).
4. Whether `repo_root` scoping should support multi-repo questions (v2.0
   proposal: one repo per request; multi-repo = multiple requests).
5. Regeneration script for the 62-task eval set (entities are real but the
   original `tasks.jsonl` is gone).
