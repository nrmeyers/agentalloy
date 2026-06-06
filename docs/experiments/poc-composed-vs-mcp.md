# POC: Composed vs MCP Skill Dictionary

## Executive Summary

> **Goal: Targeted JIT composition beats agent-decided skill fetching.**
> The agent shouldn't have to figure out which skills to call — the deterministic
> signal layer does it automatically.

This document describes the proof-of-concept experimental design comparing
AgentAlloy's composed (just-in-time) skill injection against the MCP skill
dictionary pattern and a no-injection baseline. The MCP skill-dictionary
pattern (agent has all skills as tools, calls them on demand) is the actual
competitive approach used by real harnesses.

**The claim:** removing the agent's decision burden while preserving (or
improving) output quality is the win.

Run the experiment yourself:

```bash
AGENT_MODEL=<your-agent-model> uv run python -m eval.run_poc --n 3
```

Requires a running AgentAlloy service (`http://localhost:47950`) and a local
agent model via LM Studio (`http://localhost:1234`).

---

## 1. Hypothesis

The MCP skill-dictionary pattern gives the agent a tool (`get_skill(skill_id)`)
that returns full skill files on demand. The agent must:
1. Introspect the task
2. Decide which skills to fetch
3. Call the tool for each skill
4. Incorporate the results

AgentAlloy eliminates steps 1-3 entirely. The deterministic signal layer
detects the phase/task and composes targeted skills automatically.

**Hypothesis:** smaller models benefit more from JIT composition because they
have less inherent knowledge and rely more on injected skills. The MCP arm
tests whether the agent can identify relevant skills — a harder task for
smaller models.

---

## 2. Experimental Design

**Three arms:**

- **No injection** — Raw task spec only. No skills, no governance, no injection.
  Baseline for what the agent can do with training alone.

- **MCP** — Agent has a skill dictionary and must decide which skills to fetch.
  Full skill files are fetched on demand. This simulates the real competitive
  pattern where the agent has all skills as tools but must figure out which
  to call.

- **Composed** — AgentAlloy's `/compose` endpoint returns targeted, phase-aware
  skill excerpts automatically injected as the system prompt.

**Agent model:** Both arms call the same local agent model (default:
`qwen/qwen2.5-coder-14b`) via OpenAI-compatible `/v1/chat/completions` on
LM Studio. Temperature=0.2, max_tokens=4096, deterministic seed per
task/condition/run.

**Runs:** 3 runs per task per condition (default `--n 3`). Seeds are
deterministic: `abs(hash((task_id, condition, run_index))) % 2**31`.

---

## 3. Metrics

For each run:

- **Input tokens** — `prompt_tokens` from the model's response
- **Output tokens** — `completion_tokens` from the model's response
- **Total tokens** — input + output
- **Agent latency** — wall clock from request to completion (ms)
- **Compose latency** — `/compose` call time (composed arm only)
- **Wall clock** — compose + agent latency (composed), agent only (mcp/no_inject)
- **Tokens per second** — total_tokens / wall_seconds (effective throughput)

For each task, per condition:

- **Mean score** — average of per-criterion pass rates across runs
- **Passes** — number of runs with score=1.0 (all criteria met)

Cross-condition deltas:

- **Delta score** — composed mean_score minus other arm mean_score
- **Token savings** — how many fewer tokens composed uses vs other arm
- **Wall clock savings** — how much faster composed runs vs other arm

---

## 4. Tasks

10 pre-registered tasks with deterministic graders. Each task specifies a
spec, phase, and gold_skills. The grader checks binary criteria; score =
fraction of criteria passed.

### Phase 1 tasks (original 5)

| Task | Phase | Gold Skills | Criteria |
|------|-------|-------------|----------|
| `task_1_tdd_failing_test` | build | test-driven-development | Parses as Python, has test function, imports calculate_tax, uses pytest style, has edge case |
| `task_2_bugfix_commit` | build | git-workflow-and-versioning, debugging-and-error-recovery | Conventional commit subject, under 70 chars, describes root cause, mentions test evidence |
| `task_3_code_review_checklist` | qa | code-review-and-quality | Exactly 5 items, addresses authorization, addresses safety, addresses input validation |
| `task_4_flaky_ci_debug` | qa | debugging-and-error-recovery, test-driven-development | Mentions isolation technique, hypothesizes root cause, not just retry, under 400 words |
| `task_5_browser_test_plan` | qa | browser-testing-with-devtools, code-review-and-quality | 2+ testing dimensions, names a concrete tool, specifies capture strategy |

### Phase 2 tasks (added 2026-04-25)

| Task | Phase | Gold Skills | Criteria |
|------|-------|-------------|----------|
| `task_6_phone_regex` | build | api-and-interface-design, security-and-hardening | Regex compiles, matches all 3 formats, discusses all 3 formats, non-trivial pattern |
| `task_7_friday_deploy_risks` | ops | deprecation-and-migration, security-and-hardening | Exactly 3 numbered items, mentions rollback, mentions traffic/load, mentions team availability |
| `task_8_postmortem` | qa | debugging-and-error-recovery, documentation-and-adrs | Has timeline section, has root cause section, has action items section, mentions connection pool, under 600 words |
| `task_9_retry_strategy` | design | api-and-interface-design, debugging-and-error-recovery | Covers retry budget, covers backoff, covers idempotency key, covers when to give up |
| `task_10_db_perf_runbook` | qa | debugging-and-error-recovery, performance-optimization, documentation-and-adrs | Has triage section, has root causes section, has fix strategies section, has rollback section, has communication section |

---

## 5. Implementation

**Harness:** `eval/run_poc.py`

**Key functions:**

- `run_composed(client, task, run_index, out_dir, k)` — POSTs to `/compose`,
  uses returned output as system prompt
- `run_mcp(client, task, run_index, out_dir)` — loads all skills into a
  dictionary, gives agent system prompt telling it it has a `get_skill()`
  tool, injects gold skills to simulate correct MCP calls
- `run_no_inject(client, task, run_index, out_dir)` — raw task spec only
- `call_agent(client, system, user, seed)` — POSTs to LM Studio
  `/v1/chat/completions`
- `aggregate(results)` — computes per-task and total summaries with deltas

**Output:** Results written to
`eval/runs/<timestamp>/<task_id>/<condition>/run-<N>.{txt,meta.json}`.
Summary written to `eval/runs/<timestamp>/summary.json`.

---

## 6. Recall Harness

Separate from the agent evaluation, `eval/recall.py` (now integrated into
`eval.layers.retrieval_quality`) measures retrieval-only recall@k without
any agent model. For each task, it calls `/compose` and checks how many
gold_skills appear in the returned `source_skills`. Reports micro-averaged
and macro-averaged recall plus full-recall count.

Usage:

```bash
uv run python -m eval.layers.retrieval_quality --k 4
```

This is the primary tool for A/B testing embedding-side changes (new model,
diversity settings, BM25 weighting) without agent-side variance.

---

## 7. Success Criteria

### Layer 2 (Composed vs MCP)

- `delta_score_composed_minus_mcp` > 0 (composed beats MCP in correctness)
- `token_savings_pct` > 0 (composed uses fewer tokens than MCP)
- `wall_clock_savings_pct` > 0 (composed runs faster than MCP)
- `delta_score_composed_minus_no_inject` > 0 (injection adds value over training)

### Layer 3 (Cross-Model)

- `composed` > `no_inject` for all model sizes
- The gap between `composed` and `no_inject` is largest for the small model
- `composed` ≥ `mcp` for all model sizes (especially small)

### Layer 4 (Idempotency)

- `all_pass` = true (100% deterministic across 100 runs)
- `avg_compose_ms` < 100ms

### Layer 5 (Session Simulation)

- `total_savings_pct` ≥ 30% (composed uses 30% fewer tokens across the session)
- Per-step savings increase as phases accumulate

---

## 8. Running the Experiment

```bash
# Start AgentAlloy service
uv run python -m agentalloy serve

# In another terminal, start LM Studio with your agent model
# Then run the POC:
AGENT_MODEL=qwen/qwen2.5-coder-14b uv run python -m eval.run_poc --n 3

# Or test a single task:
uv run python -m eval.run_poc --n 3 --task task_1_tdd_failing_test

# Or test only specific conditions:
uv run python -m eval.run_poc --n 3 --conditions composed mcp
```

Environment variables:
- `AGENTALLOY_URL` — AgentAlloy service URL (default: `http://localhost:47950`)
- `LM_STUDIO_URL` — LM Studio URL (default: `http://localhost:1234`)
- `AGENT_MODEL` — Agent model name (default: `qwen/qwen2.5-coder-14b`)

---

## 9. Interpreting Results

The summary output shows per-task and total comparisons:

```
task_1_tdd_failing_test
  composed     score=0.80 passes=3/3 in=  512 out= 1024 total= 1536 wall= 3200ms tps=0.5
  mcp          score=0.60 passes=2/3 in=  768 out=  896 total= 1664 wall= 4100ms tps=0.4
  no_inject    score=0.40 passes=1/3 in=  256 out=  640 total=  896 wall= 2800ms tps=0.3

  → Δscore(comp - mcp)=-0.20  composed uses 8% fewer tokens vs MCP  composed runs 22% faster vs MCP
  → Δscore(comp - no_inject)=0.40
```

Key columns:
- `score` — mean fraction of criteria passed (0.0 to 1.0)
- `passes` — runs with perfect score
- `in/out/total` — token counts
- `wall` — wall clock in milliseconds
- `tps` — effective throughput (total tokens / wall seconds)

---

## 10. Known Limitations

1. **Single model** — results are specific to the agent model used. Different
   models may show different patterns.
2. **Small task set** — 10 tasks provide a signal but not statistical
   significance. More tasks would strengthen the conclusion.
3. **Local hardware** — wall clock times depend on the specific GPU/CPU used.
   Token ratios are more portable.
4. **Temperature 0.2** — low temperature reduces variance but may not reflect
   real-world usage.
5. **MCP arm is oracle-simulated** — the MCP arm injects gold skills directly
   (simulating the agent correctly identifying and fetching them). In reality,
   the agent might pick wrong skills. This means the MCP arm is actually
   *favorable* — if composed still beats it, the signal is stronger.

---

## 11. Future Directions

- **More tasks** — expand from 10 to 50+ tasks across additional domains
- **Multiple models** — run the same experiment on different agent models to
  assess generalizability
- **Production workload** — measure composition overhead in real coding
  sessions, not just controlled tasks
- **A/B on live repos** — compare actual PR quality between JIT and MCP patterns
- **Non-oracle MCP** — truly simulate the agent's tool-calling behavior to
  measure the full overhead of agent-decided skill fetching

---

## 12. References

- `eval/run_poc.py` — POC harness implementation
- `eval/tasks.py` — Task definitions and graders (10 tasks, 372 lines)
- `eval/layers/retrieval_quality.py` — Retrieval-only recall@k harness
- `src/agentalloy/orchestration/compose.py` — Composition engine
- `src/agentalloy/retrieval/domain.py` — Hybrid BM25 + dense retrieval
- `src/agentalloy/applicability.py` — Phase/category applicability filtering
