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

Corpus provenance: the skill content in `src/agentalloy/_packs/` —
especially the domain packs — is distilled from vendor `llms.txt`/`llms.md`
documentation (Temporal, dbt, GitHub, etc.), not authored against these
eval tasks. The tasks and graders are in-house; the skill prose the
retrieval pipeline selects from is not.

| Model | Architecture | None | Composed | Flat | Composed vs flat |
|-------|--------------|------|----------|------|------------------|
| Qwen3.6-35B-A3B | MoE (~3B active) | 0.92 | **0.93** | 0.91 | −19% tokens, −18% wall |
| Qwen3.6-27B | dense | 0.86 | 0.90 | **0.96** | −16% tokens, −13% wall |
| Gemma 4 12B IT | dense | 0.85 | 0.84 | **0.88** | −12% tokens, −2% wall |
| LFM2.5-8B-A1B (coder) | hybrid sparse (1.5B active) | 0.80 | **0.85** | 0.80 | −21% tokens, −21% wall |

### Two findings worth singling out

**A 1.5B-active edge model with composed skills matches a bare 27B dense
model — at 12.6× the speed.** LFM2.5-8B-A1B + composed injection scored
0.850 vs the bare Qwen3.6-27B's 0.855 (a noise-level gap), completing the
full 30-call leg in 42.2s vs 533.6s. The LFM runs comfortably on consumer
hardware — laptops, mini-PCs, NPUs — where a 27B dense model doesn't. For
on-device agents, composed injection buys mid-size-model quality at edge
cost.

**Composed injection made the edge model both better *and* faster than
itself.** LFM2.5 with composed skills beat its own no-skill baseline on
quality (+0.05) while finishing 29% faster — the focused skill prose cut
its output rambling nearly in half (12.4K vs 19.0K output tokens). Flat
injection of the same skills' full prose delivered zero quality lift on
this model. Targeted context doesn't just inform a small model; it
disciplines it.

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

**Replication note.** After a retrieval-hardening pass (phase-eligibility
fixes, corpus-wide tagging), the 35B and 27B composed legs were rerun
end-to-end: 0.93 → 0.92 and 0.90 → 0.915 respectively — both within
noise. The generic numbers above are stable across the fix.

### Domain tasks (2026-06-10)

The 10 generic tasks measure general software-engineering competence,
where strong models are near ceiling without help. A second pre-registered
set (`eval/domain_tasks.py`, 8 tasks × 3 seeded runs) targets what the
corpus actually carries: pack conventions — webhook signature/dedup/DLQ
handling, Temporal determinism, GitHub Actions OIDC, dbt incremental
models, SCD Type 2. Same conditions; note **flat is an oracle arm** here —
it hand-injects exactly the task's gold skills, the ceiling automatic
retrieval is chasing.

| Model | None | Composed | Flat (oracle) | Composed lift | Composed tok/s |
|-------|------|----------|---------------|---------------|----------------|
| Qwen3.6-35B-A3B | 0.86 | **0.99** | 1.00 | +0.13 | 319 |
| Qwen3.6-27B | 0.88 | **0.97** | 1.00 | +0.10 | 105 |
| Gemma 4 12B IT | 0.91 | **0.98** | 0.98 | +0.07 | 210 |
| LFM2.5-8B-A1B (coder) | 0.62 | **0.81** | 0.83 | +0.19 | 779 |

Findings, stated as measured:

- **Composed beat the bare model on every architecture** (+0.07 to +0.19),
  and the weaker the model, the bigger the lift. On conventions the model
  doesn't ship with, injection is the difference between guessing and
  knowing.
- **Automatic retrieval lands within 0.01–0.03 of the hand-picked
  oracle** on all four models, at roughly equal token cost (±7%). Skill
  selection is not the bottleneck; residual gaps are model capacity.
- **A 12B dense model with composed skills tied a 27B dense model**
  (0.975 vs 0.971) at 2× the throughput and 2.5× faster wall-clock
  (210 vs 105 tok/s; 148s vs 364s for the full leg). Bare Gemma scored
  0.906 — composition closed the gap to the next weight class.
- **Capacity still matters at the low end.** Composed LFM2.5 (0.81)
  did not match the bare 27B (0.88) on domain tasks the way it did on
  generic ones; convention-heavy work rewards parameters as well as
  context.

Caveats (both task sets): heuristic binary graders measure surface
criteria, not depth; n=3 per cell; single host; quants differ per model.
Treat deltas under ~0.05 as noise.

Reproduce a leg:

```bash
AGENT_MODEL=<model-id> LM_STUDIO_URL=<http://host:port> \
  uv run python -m eval.run_poc --n 3                  # composed + flat
AGENT_MODEL=<model-id> LM_STUDIO_URL=<http://host:port> \
  uv run python -m eval.run_poc --n 3 --conditions none --label baseline
AGENT_MODEL=<model-id> LM_STUDIO_URL=<http://host:port> \
  uv run python -m eval.run_poc --n 3 --task-set domain --label domain \
  --conditions composed flat none                      # domain set
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
