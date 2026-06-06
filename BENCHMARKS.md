# Benchmarks

## Overview

AgentAlloy benchmarks are organized into 5 layers, each measuring a different
aspect of the system's effectiveness. Run all layers or pick individual ones:

```bash
uv run python -m eval.benchmark              # all layers
uv run python -m eval.benchmark --layer 1     # retrieval quality only
uv run python -m eval.benchmark --dry-run     # show what would run
```

### Layers

| Layer | Name | Needs agent model? | What it proves |
|-------|------|--------------------|----------------|
| 1 | Retrieval quality | No | Recall@k, precision@k, MRR, phase contamination |
| 2 | Composed vs MCP | Yes | Targeted JIT beats agent-decided skill fetching |
| 3 | Cross-model robustness | Yes | Quality improvement generalizes across model sizes |
| 4 | Idempotency | No | Deterministic composition (same task → same output) |
| 5 | Session simulation | No | Token savings grow as session accumulates across phases |

---

## The Three Experimental Arms

Every benchmark task runs across three conditions:

| Arm | What the agent sees | What it tests |
|-----|---------------------|---------------|
| **composed** | AgentAlloy's `/compose` endpoint — targeted, phase-aware skill excerpts injected automatically | JIT composition quality |
| **mcp** | Agent has a skill dictionary and must decide which skills to fetch via a `get_skill()` tool | Agent's ability to identify relevant skills (the real competitive pattern) |
| **no_inject** | Raw task spec only — no skills, no governance, no injection | What the model can do with training alone |

### Why this matters

The MCP skill-dictionary pattern (agent has all skills as tools, calls them on demand) is the actual competitive approach used by real harnesses. AgentAlloy's claim is that **removing the agent's decision burden while preserving (or improving) output quality** is the win.

If smaller models consistently fail to call the right MCP tools, that's the proof point. AgentAlloy eliminates the need for the agent to figure out which skills to fetch — the deterministic signal layer does it automatically.

---

## Composed vs MCP (Layer 2)

The POC comparing AgentAlloy's just-in-time composed injection against the
MCP skill-dictionary pattern is the core benchmark.

**Hypothesis:** targeted JIT composition (composed) beats the MCP pattern
because the agent doesn't have to figure out which skills to fetch. The MCP
arm tests whether the agent can introspect and identify relevant skills —
a burden AgentAlloy removes entirely.

**Success criteria:**
- `composed` > `mcp` in correctness (positive delta_score)
- `composed` < `mcp` in tokens (positive token_savings_pct)
- `composed` < `mcp` in wall clock (positive wall_clock_savings_pct)
- `composed` > `no_inject` in correctness (injection adds value)

### Run the experiment

```bash
# Start AgentAlloy service (one terminal)
cd /home/nmeyers/dev/agentalloy
uv run python -m agentalloy serve &

# Start agent model (LM Studio example — another terminal)
# Open LM Studio → load qwen/qwen2.5-coder-14b → start server on :1234

# Run the full experiment (3 arms x 10 tasks x 3 runs = 90 agent calls)
uv run python -m eval.run_poc --n 3

# Or via the orchestrator:
uv run python -m eval.benchmark --layer 2

# Run with a specific model:
AGENT_MODEL=qwen/qwen2.5-coder-14b uv run python -m eval.run_poc --n 3

# Run only specific conditions:
uv run python -m eval.run_poc --conditions composed mcp --n 3

# Run a single task:
uv run python -m eval.run_poc --task task_1_tdd_failing_test --n 3
```

### Results

Output goes to `eval/runs/<timestamp>/` with:
- `summary.json` — aggregated scores, deltas, and token counts
- `manifest.json` — run configuration and environment
- Per-task/condition directories with individual outputs and metadata

### Interpreting Layer 2 results

```
task_1_tdd_failing_test
  composed     score=0.80 passes=3/3 in=  512 out= 1024 total= 1536 wall= 3200ms tps=0.5
  mcp          score=0.60 passes=2/3 in=  768 out=  896 total= 1664 wall= 4100ms tps=0.4
  no_inject    score=0.40 passes=1/3 in=  256 out=  640 total=  896 wall= 2800ms tps=0.3

  → Δscore(comp - mcp)=-0.20  composed uses 8% fewer tokens vs MCP  composed runs 22% faster vs MCP
  → Δscore(comp - no_inject)=0.40
```

**Key numbers:**
- `delta_score_composed_minus_mcp` — positive means composed beats MCP
- `token_savings_pct` — how many fewer tokens composed uses vs MCP
- `wall_clock_savings_pct` — how much faster composed runs vs MCP
- `delta_score_composed_minus_no_inject` — positive means injection adds value

---

## Retrieval Recall (Layer 1)

Measures retrieval quality without any agent model. Tests whether the
hybrid BM25+dense retrieval finds the right skills for each task.

```bash
uv run python -m eval.benchmark --layer 1
# Or directly:
uv run python -m eval.layers.retrieval_quality --k 4
```

**Success criteria:**
- `micro_recall` ≥ 0.7 (at least 70% of gold skills found)
- `micro_precision` ≥ 0.6 (at least 60% of retrieved skills are relevant)
- `mean_mrr` ≥ 0.8 (first gold skill appears near the top)
- `contamination` = 0 (no skills from wrong phases)

---

## Cross-Model Robustness (Layer 3)

Tests whether the quality improvement from JIT composition generalizes
across model sizes.

```bash
uv run python -m eval.benchmark --layer 3
# Or directly:
uv run python -m eval.layers.cross_model
```

By default tests 3 models:
- **small:** `qwen/qwen2.5-coder-1.5b`
- **medium:** `qwen/qwen2.5-coder-14b`
- **large:** `meta-llama/llama-3.1-70b`

Override with environment variables:
```bash
MODEL_SMALL_URL=http://s:1234 MODEL_SMALL_NAME=qwen/1.5b \
MODEL_LARGE_URL=http://l:1234 MODEL_LARGE_NAME=llama/70b \
uv run python -m eval.layers.cross_model
```

**Hypothesis:** smaller models benefit more from JIT composition because
they have less inherent knowledge and rely more on injected skills.

**Success criteria:**
- `composed` > `no_inject` for all model sizes
- The gap between `composed` and `no_inject` is largest for the small model
- `composed` ≥ `mcp` for all model sizes (especially small)

---

## Composition Idempotency (Layer 4)

Proves the deterministic claim: same task → same composition, regardless
of which agent model you swap in tomorrow.

```bash
uv run python -m eval.benchmark --layer 4
# Or directly:
uv run python -m eval.layers.idempotency --n 100
```

Sends identical POST requests to `/compose` 100 times per task and checks
byte-identical output.

**Success criteria:**
- `all_pass` = true (100% deterministic across 100 runs)
- `avg_compose_ms` < 100ms

---

## Session Simulation (Layer 5)

Simulates a real coding session that transitions through SDD phases
(spec → design → build → qa) with multiple tasks per phase.

```bash
uv run python -m eval.benchmark --layer 5
# Or directly:
uv run python -m eval.layers.session_simulation
```

**Hypothesis:** token savings grow as the session accumulates across phases.
The MCP arm requires the agent to fetch skills at each step, accumulating
tokens. The composed arm gets only the relevant skills for the current step.

**Success criteria:**
- `total_savings_pct` ≥ 30% (composed uses 30% fewer tokens across the session)
- Per-step savings increase as phases accumulate (flat/MCP degrades)
- `composed_score` ≥ `no_inject` score at every step

---

## Full Benchmark Suite

To run the complete 5-layer benchmark:

```bash
# 1. Start services
uv run python -m agentalloy serve &

# 2. Start agent model (LM Studio or Ollama)

# 3. Run everything
uv run python -m eval.benchmark

# 4. View results
cat eval/runs/benchmark__*/summary.json | python -m json.tool
```

### Expected runtime (warm machine, 14B model, 3 runs/task)

| Layer | Time | Needs agent model? |
|-------|------|--------------------|
| 1 | ~30s | No |
| 2 | ~15m | Yes (90 agent calls) |
| 3 | ~45m | Yes (270 agent calls across 3 models) |
| 4 | ~2m | No (1000 compose calls) |
| 5 | ~1m | No |
| **Total** | **~60m** | |

With 10 runs/task, multiply layer 2 and 3 times ~3.

---

## Benchmark Architecture

```
eval/
├── benchmark.py          # Orchestrator — runs all layers, produces unified report
├── __main__.py           # python -m eval entry point
├── run_poc.py            # POC harness — runs composed/MCP/no_inject arms
├── tasks.py              # Task definitions + per-task graders
├── layers/
│   ├── retrieval_quality.py      # L1: recall@k, precision@k, MRR, contamination
│   ├── composed_vs_flat.py       # L2: orchestrates POC, computes deltas
│   ├── cross_model.py            # L3: runs all arms across model lineup
│   ├── idempotency.py            # L4: 100 identical compose calls, byte check
│   └── session_simulation.py     # L5: multi-phase session, token savings
```

---

## Adding New Tasks

Tasks are defined in `eval/tasks.py`. Each task has:
- `task_id`: unique identifier
- `spec`: natural-language task description
- `phase`: SDD phase (spec/design/build/qa/ops)
- `gold_skills`: tuple of skill IDs the task should retrieve
- `grade_task_N()`: function that returns `{criterion: bool}` for each task

To add a task:
1. Add a `Task` to `TASKS` in `eval/tasks.py`
2. Write a `grade_task_N()` function with pass/fail criteria
3. Register it in `GRADERS` dict

---

## License

MIT. See [LICENSE](LICENSE).
