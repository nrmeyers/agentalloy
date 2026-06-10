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
skill injection and a no-injection baseline. The pre-registered tasks and
binary graders live in `eval/tasks.py`; the harness is `eval/run_poc.py`.

### Measured results (2026-06-10)

Setup: 10 pre-registered tasks × 3 seeded runs per condition, `k=4`,
graders are deterministic binary criteria (`eval/tasks.py`). Agent models
served by llama.cpp (GGUF quants) on a dedicated single-GPU host, one
request at a time. Conditions: **composed** (skills assembled per-task by
`/compose`), **flat** (the task's gold skills' full prose), **none**
(bare system prompt, no skills).

| Model | Architecture | None | Composed | Flat | Composed vs flat |
|-------|--------------|------|----------|------|------------------|
| Qwen3.6-35B-A3B | MoE (~3B active) | 0.92 | **0.93** | 0.91 | −19% tokens, −18% wall |
| Qwen3.6-27B | dense | 0.86 | 0.90 | **0.96** | −16% tokens, −13% wall |
| Gemma 4 12B IT | dense | 0.85 | 0.84 | **0.88** | −12% tokens, −2% wall |
| LFM2.5-8B-A1B (coder) | hybrid sparse (1.5B active) | 0.80 | **0.85** | 0.80 | −21% tokens, −21% wall |

Findings, stated as measured:

- **Composed prompts are 17–20% smaller** than flat (gold-skills-only)
  prompts and runs complete 2–21% faster. Note the flat arm here is
  *generous* to flat: it injects only the task's 2–3 gold skills. Flat
  injection of a whole pack or corpus — the practice composed injection
  replaces — would be far larger.
- **Sparse architectures favor composed.** On the MoE 35B and the
  1.5B-active LFM2.5, composed beat both flat and baseline. On LFM2.5,
  flat injection delivered *zero* lift over no skills at all (0.80 both)
  while composed delivered +0.05 — small attention budgets get swamped by
  flat prose.
- **Mid-size dense models favor flat** on raw score (27B: 0.96 vs 0.90;
  Gemma: 0.88 vs 0.84), paying 12–16% more tokens for it. The 27B is the
  only model where skill injection of either kind produced a large lift
  over baseline (+0.10 flat, +0.04 composed).
- **Strong models are near ceiling on generic tasks.** The 35B and Gemma
  baselines sit within ±0.04 of their injected scores. These 10 tasks are
  general software-engineering tasks; the corpus's domain packs
  (webhooks, temporal, snowflake conventions, …) target knowledge models
  don't ship with, which these tasks do not measure.

Caveats: heuristic binary graders measure surface criteria, not depth;
n=3 per cell; single host; quants differ per model. Treat deltas under
~0.05 as noise.

Reproduce a leg:

```bash
AGENT_MODEL=<model-id> LM_STUDIO_URL=<http://host:port> \
  uv run python -m eval.run_poc --n 3                  # composed + flat
AGENT_MODEL=<model-id> LM_STUDIO_URL=<http://host:port> \
  uv run python -m eval.run_poc --n 3 --conditions none --label baseline
```

Requires a running AgentAlloy service and an agent model behind any
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
