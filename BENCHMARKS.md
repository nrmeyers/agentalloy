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

The five layers above measure the **composition** path (task → skills). The
**signals / phase-gate layer** — which decides SDD phase transitions from user
intent — is benchmarked separately; see
[Intent Classification](#intent-classification-signals-layer).

---

## Composed vs Flat (Layer 2)

AgentAlloy's just-in-time composed injection is compared against flat skill
injection and a no-injection baseline. The pre-registered tasks and
deterministic binary graders live in `eval/tasks.py` (generic) and
`eval/domain_tasks.py` (domain); the harness is `eval/run_poc.py`.

The durable result lives in the **domain** set — convention-heavy tasks the
corpus actually carries knowledge for. The **generic** set is a near-ceiling
regression check (strong models need no help on general software tasks) and is
reported second, read accordingly.

Corpus provenance: the skill content in `src/agentalloy/_packs/` — especially
the domain packs — is distilled from vendor `llms.txt`/`llms.md` documentation
(Temporal, dbt, GitHub, etc.), not authored against these eval tasks. The tasks
and graders are in-house; the skill prose the retrieval pipeline selects from is
not.

### Domain tasks (v3 campaign, 2026-06-12/13)

The pre-registered domain set (`eval/domain_tasks.py`, 18 tasks × 5 seeded runs
per condition) targets pack conventions — webhook signature/dedup/DLQ handling,
Temporal determinism, GitHub Actions OIDC, dbt incremental models, SCD Type 2,
Redis streams/locks, Snowflake/Redshift, OTel trace propagation. Conditions:
**composed** (skills assembled per task by `/compose`), **flat** (an *oracle*
arm — hand-injects exactly the task's gold skills, the ceiling automatic
retrieval chases), **none** (bare system prompt). Graders are deterministic
binary criteria, de-brittled in #141 to credit synonyms/paraphrase. Composition
runs the shipped deterministic Stage-0 config (card-indexed corpus,
`LM_ASSIST=off`, `RETRIEVAL_GRAPH_EXPAND=off`).

| Model | None | Composed | Flat (oracle) | Composed lift | % of oracle | Tokens vs flat |
|-------|------|----------|---------------|---------------|-------------|----------------|
| Qwen3.6-35B-A3B | 0.937 | **0.976** | 0.992 | +0.039 | 71% | −21% |
| Qwen3.6-27B | 0.958 | **0.980** | 0.989 | +0.022 | 71% | −21% |
| Gemma 4 12B IT | 0.925 | **0.945** | 0.964 | +0.020 | 51% | −32% |
| LFM2.5-8B-A1B (coder) | 0.657 | **0.829** | 0.902 | +0.172 | 70% | −22% |

Findings, stated as measured:

- **Composed beat the bare model on every architecture** (+0.020 to +0.172),
  and the weaker the model, the bigger the lift. The LFM2.5 edge model gains
  **+0.172** — on conventions a model doesn't ship with, injection is the
  difference between guessing and knowing.
- **Automatic retrieval captures ~51–71% of the perfect-knowledge oracle**
  ((composed−none)/(flat−none)), landing within 0.009–0.073 of the hand-picked
  flat arm at 21–32% fewer tokens. Selection is not the bottleneck on the strong
  models; the residual gap is model capacity (widest on LFM, −0.073 below
  oracle).
- **Capacity still matters at the low end.** Composed LFM2.5 (0.829) does not
  reach the bare 27B (0.958) on domain tasks; convention-heavy work rewards
  parameters as well as context. (An earlier campaign's "edge model matches a
  bare 27B" equivalence was a generic-task artifact that did not replicate at
  v3 — see the generic set below.)

**Why the optional LM stages are off in this campaign.** The composed arm above
runs the deterministic Stage-0 config on purpose: the two optional composition
levers were measured on the LFM domain leg and neither beat it. The **fragment
re-ranker** (`LM_ASSIST=arbitrate`) scored 0.827 in its as-shipped config —
exactly tying the deterministic baseline (0.827) — and trailed slightly
(0.809–0.817) once the candidate pool was widened; its canary task `domain_1`
did not recover (0.76, dropping to 0.48 in the top-12 variant). **Graph
expansion** (`RETRIEVAL_GRAPH_EXPAND=on`) also tied (0.827). Both stay off by
default on that evidence. The signals-layer intent backend, which *did* win its
benchmark, is the one model-backed stage shipped on (see
[Intent Classification](#intent-classification-signals-layer)).

#### Judge-validated fidelity (27B LLM-judge, 2026-06-13)

To confirm the composed lift is real answer quality and not an artifact of the
literal-substring graders, the LFM and 12B domain outputs were independently
re-graded by a local LLM judge (qwen3.6-27b, scalar rubric) — 540 judgments,
0 parse errors.

| Model | None | Composed | Flat | Composed−none (judge) | (heuristic) | % of oracle |
|-------|------|----------|------|------------------------|-------------|-------------|
| LFM2.5 | 0.651 | 0.805 | 0.838 | **+0.154** | +0.172 | 83% |
| Gemma 12B | 0.979 | 0.993 | 0.997 | **+0.013** | +0.020 | 75% |

Pooled composed−none = +0.084, bootstrap 95% CI **[+0.056, +0.114]** (excludes
zero). The independent judge **confirms** the lift and runs slightly
*conservative* versus the length-blind heuristic on both models — it does not
inflate. A length-bias diagnostic (judge score vs output length, Pearson
−0.685) cuts *in our favor*: `none` is the **longest** condition (2604 tokens
vs composed 1988 / flat 1851), so the bias would inflate composed−none — yet the
judge still finds a *smaller* lift than the length-blind heuristic. The lift is
real quality, not verbosity; `none`'s extra length is itself a tell that
unguided answers ramble. Judge–heuristic Pearson is 0.542 over the full set —
moderate at the item level; the load-bearing signal is the convergence on the
*delta*, not row-by-row agreement.

### Generic tasks (regression check)

The generic set (`eval/tasks.py`) measures general software-engineering
competence, where strong models sit near ceiling without help. At v3 only the
LFM and 12B legs were rerun; the 35B/27B rows are the prior (v2) campaign. The
set has no oracle (flat) arm, so only composed-vs-none is reported.

| Model | None | Composed | Composed−none | Source |
|-------|------|----------|----------------|--------|
| Qwen3.6-35B-A3B | 0.961 | 0.955 | −0.006 | v2 |
| Qwen3.6-27B | 0.939 | 0.940 | +0.001 | v2 |
| Gemma 4 12B IT | 0.868 | 0.892 | +0.024 | v3 |
| LFM2.5-8B-A1B (coder) | 0.852 | 0.828 | −0.024 | v3 |

Findings, stated as measured:

- **Near ceiling, as expected.** The strong models move ±0.006 — composition
  neither helps nor hurts on general tasks they already handle.
- **The edge model regresses slightly on generic tasks** (−0.024). The focused
  skill prose that *disciplines* LFM2.5 on convention-heavy domain work is, on
  already-simple generic tasks, context it doesn't need — a mild distractor.
  This is precisely why the headline is the domain set, not this one.
- **An earlier cross-class equivalence did not replicate.** A prior campaign
  showed generic LFM2.5+composed (~0.85) ≈ bare 27B (~0.855); at v3 generic
  LFM2.5 composed is 0.828 and bare 27B is 0.939. We retired the claim rather
  than reframe it.

Caveats (both sets): heuristic binary graders measure surface criteria, not
depth — the 27B judge pass above is the cross-check; n=5 per cell on domain;
single host; quants differ per model. Treat deltas under ~0.05 as noise on the
strong models.

Reproduce a leg:

```bash
AGENT_MODEL=<model-id> LM_STUDIO_URL=<http://host:port> \
  uv run python -m eval.run_poc --n 5                  # composed + flat
AGENT_MODEL=<model-id> LM_STUDIO_URL=<http://host:port> \
  uv run python -m eval.run_poc --n 5 --conditions none --label baseline
AGENT_MODEL=<model-id> LM_STUDIO_URL=<http://host:port> \
  uv run python -m eval.run_poc --n 5 --task-set domain --label domain \
  --conditions composed flat none                      # domain set
```

Requires a running AgentAlloy service and an agent model behind any
OpenAI-compatible endpoint (LM Studio, llama-server).

## Retrieval Recall (Layer 1)

The recall@k harness measures retrieval quality without any agent model:

```bash
uv run python -m eval.recall --k 4
```

Gold skills per task are defined in `eval/tasks.py` against the bundled pack
corpus (`src/agentalloy/_packs/`).

## Intent Classification (signals layer)

Orthogonal to the composition layers above: the signals layer wakes on prompts
and decides SDD phase transitions (`spec → design → build → qa → ship`) by
classifying user utterances against named transition intents (completion /
approval / redirection). This benchmark measures that classifier, not retrieval.

```bash
uv run python -m eval.intent_bench          # needs llama-server embed :47951 + reranker :60001
```

Two backends, selected by `SIGNAL_INTENT_BACKEND` (**default `reranker`**, on the
result below; `cosine` opts out and remains the fail-open floor):

- **cosine** — embeds the utterance and takes max cosine vs per-intent reference
  phrases (operating threshold 0.75).
- **reranker** — qwen3-reranker-0.6b scores the utterance-as-query against each
  intent's task description, with a deterministic negation guard (`_has_negation`)
  that vetoes negated cues ("not done", "don't approve") before scoring. Cosine
  remains the **fail-open floor**: a disabled / unreachable / failed reranker, or
  an intent with no task description, falls through to cosine byte-for-byte.

### Results (2026-06-13, 111 labeled utterances)

Decided in the **per-intent framing** — each runtime gate queries exactly one
intent, so the decision never sees the other intents' scores (no argmax):

| metric | cosine | reranker (no guard) | reranker (+guard) |
|---|---|---|---|
| macro-F1 (per-intent) | 0.242 | 0.653 | **0.687** |
| negation-slice accuracy | 0.846 | 0.462 | **1.000** |
| overall accuracy | 0.613 | 0.766 | **0.820** |
| latency p50 (ms) | 118 | 59 | 59 |

Full report + per-utterance scores: `eval/runs/intent-bench-2026-06-13/`.

**Operating threshold.** The reranker yes-probability is thresholded at **0.45**,
the center of a flat 0.40–0.55 macro-F1 plateau; env-overridable via
`SIGNAL_INTENT_RERANK_THRESHOLD`.

**Non-determinism, and what pins the gate.** Raw reranker scores swing
run-to-run (llama-server continuous batching; the negation slice is small,
n=13) — the no-guard negation accuracy above is one such sample and is not
stable. The deterministic `_has_negation` guard is what pins the negation slice
to 1.0 and makes the phase gate reproducible; it recovers the one category where
the raw cross-encoder regresses *below* cosine.

**Latency framing (the comparison is conservative).** The bench scores all three
intents in one batched reranker call (59ms p50), whereas production issues one
single-doc reranker call per intent-gate via `_intent_rerank`. This understates
the reranker's production edge rather than inflating it: production cosine
re-embeds the query on every gate (`_intent_similarity` calls
`embed([query] + refs)` each time — a full llama-server round-trip per gate), so the
reranker's per-gate advantage holds and in fact widens in production.

### CPU latency and how often it fires (2026-06-13)

The reranker default has to hold on the **CPU-only container path**, where there
is no GPU. Measured on a Xeon W-2225 (4-core/8-thread @ 4.1 GHz), CPU-only
(`-ngl 0`), under concurrent load, scoring one intent gate via the production
`_intent_rerank` path (a single `/v1/completions` call):

| gate utterance | p50 | p95 | vs 300 ms budget |
|---|---|---|---|
| short (a typical phase signal) | 211 ms | 241 ms | 100% within budget |
| long (~1600-char paragraph, cold) | ~1.8 s | ~1.8 s | exceeds → times out → cosine |

Latency scales ~linearly with utterance length (~250 tok/s prefill on this CPU).
Short phase-transition signals — "looks good", "this is done", "let's change
direction" — land well inside the 300 ms fail-open budget; a long rambling prompt
exceeds it and falls open to cosine, which is the right outcome (long prose is not
a crisp phase signal).

Crucially, the gate does **not** fire every turn. A deterministic pre-filter
(`signals/prefilter.py`, <5 ms) runs first and skips the reranker entirely unless
the prompt carries a phase signal keyword (or a gate-relevant file / tool event);
on a hit, the exit-gate tree short-circuits, so the reranker is reached only for
the one or two named-intent gates the active phase actually evaluates. The CPU
cost is therefore rare and bounded, with cosine as the deterministic floor. So
the reranker ships as the default on CPU as well, with the 300 ms budget
unchanged. Weaker hosts (e.g. 2-core cloud VMs) scale latency up proportionally
and fall open to cosine more often — safe, but the lift weakens on weak CPUs.

**Status.** Measured win on a small labeled set → shipped as **the default**
backend (`SIGNAL_INTENT_BACKEND=reranker`), with cosine as the opt-out and
fail-open floor. The reranker needs a `qwen3-reranker-0.6b` server (default
`:60001`); where none is running, the gates fall open to cosine byte-for-byte, so
the default is safe but the lift is latent until the server is provisioned. Not
yet field-validated.

## Full Benchmark Suite

To run the complete 5-layer benchmark:

```bash
uv run python -m eval.benchmark
```

This produces a timestamped directory under `eval/runs/` with per-layer JSON
results and a unified summary.
