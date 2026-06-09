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
| 2 | Composed vs flat | Yes | Token savings, quality parity, speed |
| 3 | Cross-model robustness | Yes | Quality generalizes across model sizes |
| 4 | Idempotency | No | Deterministic composition (same task -> same output) |
| 5 | Session simulation | No | Context-rot argument: flat degrades across phases |

---

## Composed vs Flat (Layer 2)

The POC compares AgentAlloy's just-in-time composed injection against flat
(all-skills-at-once) injection. The pre-registered tasks and binary graders
live in `eval/tasks.py`; the harness is `eval/run_poc.py`.

**Aspirational targets** (not yet measured): 60% smaller prompts, 25% faster
runs, improved answers.

Run the experiment:

```bash
AGENT_MODEL=<your-agent-model> uv run python -m eval.run_poc --n 3
```

Requires a running AgentAlloy service and a local agent model behind any
OpenAI-compatible endpoint (LM Studio, Ollama, llama-server).

## Retrieval Recall (Layer 1)

The recall@k harness measures retrieval quality without any agent model:

```bash
uv run python -m eval.recall --k 4
```

Gold skills per task are defined in `eval/tasks.py` against the bundled pack
corpus (`src/agentalloy/_packs/`).

## Full Benchmark Suite

To run the complete 5-layer benchmark:

```bash
uv run python -m eval.benchmark
```

This produces a timestamped directory under `eval/runs/` with per-layer JSON
results and a unified summary.
