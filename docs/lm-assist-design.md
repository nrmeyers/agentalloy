# LM-assist: a sub-1B intent layer riding alongside the embedder

> **Historical design doc — kept for rationale.** For the *current* runtime configuration of the intent and LM-assist layers, see [`.env.example`](../.env.example) (§ Signal intent reranker, § LM-assist) and [operator.md](operator.md) § Signal Intent. Policy as of **v5.0.0: `LM_ASSIST` ships `off`** (fragment re-rank) on every preset — it showed no lift on the eval set (2026-06-12, reproduced by a blind judge during the v5 migration) and adds ~500 ms/compose. v4.0.2 had set it `arbitrate` to address n=2 / real-life skill-ranking issues the eval set doesn't capture; it ships off for now pending a cleaner fix for those (re-enable with `LM_ASSIST=arbitrate`). The intent reranker (`SIGNAL_INTENT_BACKEND=reranker`) — the measured win (macro-F1 0.242→0.687) — **stays on**. The rerank launcher (`start_rerank_server.rerank_launch_args`) selects hardware-appropriate slot config — `--parallel 2 -c 4096` on GPU vs `--parallel 1 -c 2048` on CPU. The CPU configuration was validated viable in Jun 2026 (Xeon W-2225, warm K=8 ~1170ms vs 2000ms budget) AFTER an empirical pass found that CPU benefits from FEWER slots (OpenMP thread contention with multiple slots hurts throughput).

Status: **shipped (current as of v3.3.5).** Originally a design sketch
(2026-06-12); the slices it scoped are now in the codebase, each fail-open to the
deterministic path. Since v2.4.0 the signals-layer intent reranker is the
**primary phase-transition trigger** (the prefilter no longer short-circuits it),
and the installer now provisions the reranker service on `:47952`. Their defaults
follow what they measured (see "How we know it works"):

- **Stage 0 — skill-card indexing** shipped (the re-embed pass indexes a
  synthetic card per skill; `agentalloy reembed --card-index` defaults to
  `both` at the CLI).
- **Stage B — fragment re-ranker** shipped (#136) as `LM_ASSIST=arbitrate`,
  using `qwen3-reranker-0.6b` — **default off** (measured no lift over
  deterministic selection).
- **Signals-layer intent backend** shipped (#142) and is now **the default**
  (`SIGNAL_INTENT_BACKEND=reranker`; `cosine` opts out and is the fail-open
  floor), reusing the Stage-B scorer — a measured win on the intent benchmark.
- **Stage A — query enrichment** is **NOT shipped (deferred).** The
  `LM_ASSIST` enum is `off|arbitrate` only; the `domains/intent/phase_hint`
  schema below describes no current code path.

The sections below preserve the original rationale; the runtime/ops and
"how we'll know it works" sections have been updated to as-built.

## Motivation

The 2026-06 campaign diagnostics surfaced one failure wearing four hats:
retrieval gets no help understanding *intent*.

1. **Rank-1 sensitivity.** Depth selection pays off when fusion ranks the
   gold skill first and doubles down when it doesn't (`domain_4`: gold
   ranked #2, composed fell 0.56 → 0.36). Fusion margins are too noisy to
   gate depth on alone.
2. **Framework ambiguity.** Underspecified queries ("build a blog
   website") never bridge to stack vocabulary; the dense leg + RRF handle
   lexical collisions but not *unstated* intent.
3. **Signal-layer brittleness.** Phase exit gates are the most
   threshold-tuned, least explainable code in the repo — classification is
   what small instruct models are for.
4. **Within-skill fragment selection.** `domain_1` (webhook signature):
   gold ranks #1 and gets both depth slots, yet composed scores 0.60 vs
   1.00 flat-oracle — the picker takes the intro fragments, not the
   `signed_content`/timing-safe-compare fragments the grader needs. Only
   fragment-level relevance can fix this. (A skill-level arbiter would
   agree with rank-1 and change nothing; this is why **Stage B arbitrates
   fragments, not skills.**)

See the eval write-up (`BENCHMARKS.md`, intent-classifier and reranker
layers) for the full diagnostics.

## Constraint: determinism is the fail-open floor

AgentAlloy's composition path is deterministic by default. Two small-local-model
stages sit alongside it — a composition fragment re-ranker (`LM_ASSIST=arbitrate`)
and a signals-layer intent backend (`SIGNAL_INTENT_BACKEND`) — each fail-open to
the deterministic path when the local model is unavailable. On any LM timeout,
error, or disabled flag, compose degrades to deterministic composition
byte-for-byte. Their defaults follow what they measured (see "How we know it
works" below): the **intent backend ships on** (`reranker`, a benchmark win;
`cosine` opts out and is the fail-open floor), while the **fragment re-ranker
ships off** (it tied — and with a wider candidate pool, slightly trailed —
deterministic selection on the domain set). Layer-4 idempotency is asserted
against the fail-open path; the LM path gets its own reproducibility story
(temp 0, pinned model tag) but no contractual guarantee. The absolute "no LLM in
the runtime path" claim was dropped 2026-06-12.

## Stage 0 — index the skill card (deterministic) — SHIPPED

Verified 2026-06-12: both retrieval legs index only fragment body text
(`frag.content` is what gets embedded and what BM25 searches). A skill's
`canonical_name`, `domain_tags`, and Overview paragraph — its
self-description — never enter the index. The corpus knows React is "for
websites"; retrieval is never told.

Fix is classic document expansion, no LM required:

- Prepend a one-line skill header to each fragment's indexed text
  (`skill: React — tags: websites, frontend, ...`), and/or
- add one synthetic "card" document per skill (name + tags + overview)
  to both legs.

Cost: one idempotent re-embed pass (minutes on the 3060). Zero runtime
latency, zero new failure modes, fully deterministic.

This plausibly closes part of both diagnosed gaps on its own
(domain_4's gold skill *name* contains the answer; the blog query can
hit framework cards).

## LM architecture

Two insertion points were scoped; only Stage B shipped.

### Stage A — query enrichment (pre-retrieval) — DEFERRED, NOT SHIPPED

Stage A was never built. The `LM_ASSIST` enum is `off|arbitrate` only;
there is no `enrich` mode and no `domains/intent/phase_hint` extraction in
the codebase. The sketch is retained for context:

```json
{
  "domains": ["nextjs", "static-site", "markdown"],   // inferred stack/domain vocabulary
  "intent": "greenfield-build",                        // closed enum
  "phase_hint": "design"                               // closed enum, advisory only
}
```

The idea was to append `domains` terms to the BM25 query as a soft boost
(never a filter). Revisit only if it earns its latency.

### Stage B — fragment re-rank (post-retrieval, pre-assembly) — SHIPPED (#136)

`LM_ASSIST=arbitrate`. Shipped as a **pairwise yes/no logprob scorer**
(`FragmentScorer`, `src/agentalloy/retrieval/lm_assist.py`), not the
JSON keep-list extractor the sketch proposed.

Mechanism: for the top `LM_ASSIST_MAX_CANDIDATES` (default 8) fused
fragments, the `qwen3-reranker-0.6b` cross-encoder is shown the task plus
one fragment at a time via `/v1/completions` using the official
Qwen3-Reranker chat template, asked whether the Document meets the Query's
requirements. Each fragment body is truncated to `LM_ASSIST_DOC_CAP_CHARS`
(default 2400, ~600 tok) first — a prefill bound that keeps fat-corpus
outliers inside the budget. It emits one token with logprobs;
`softmax(yes, no)` becomes the relevance score. (llama.cpp's `/v1/rerank`
endpoint skips the instruction template for this GGUF and is not used.)

Selection is a **filter, then diversity routing** (not a fusion-order cap):

- HIT → fragments scoring at/above `LM_ASSIST_KEEP_THRESHOLD` are the
  *survivors*; they are routed through the same depth+round-robin
  `skill_granular_select` as the deterministic path. The HIT path is no
  longer "diversity off". (Earlier it bypassed selection and assembled the
  kept fragments in fusion order, capped at k.) `keep_threshold` ships
  **truly inert and gated-off** at 0.0 — the keep test is `score >= threshold`
  and reranker yes-probabilities are in [0,1], so every scored fragment
  survives (including ones the reranker scores exactly 0.0 for a task with no
  relevant corpus coverage). The win is the restored selection routing, not
  the filter. The real prod value is a deferred decision gate pending a P(yes)
  measurement; the `LM_ASSIST_KEEP_THRESHOLD` knob ships but no preset sets it.
  (0.05 would NOT be inert — a task whose candidates all score 0.0 would be
  emptied. Live-test correction landed in v4.0.0.)
- Disabled / timeout / error / empty-survivor → deterministic depth+round-robin
  selection runs byte-for-byte as if Stage B never ran (fail-open floor).

Concurrency & ops: the per-composition fan-out (`LM_ASSIST_MAX_CANDIDATES`)
equals the reranker `--parallel` slot count (`-c 8192`), so a composition
fans out as one wave. That single knob also sizes the scorer thread pool and
bounds **both** `FragmentScorer` singletons (compose Stage B + signal
intent), so the two consumers can't oversubscribe the slots. `/health` reports
a `reranker` dependency (degraded — never unavailable — when the recent Stage B
outcome window is timeout/error-dominant), and the shared rerank failure latch
escalates its cooldown on repeated re-failures so a permanently-dead backend
quiesces instead of being probed every 60 s forever.

This subsumes the score-conditional-depth heuristic: deterministic
selection is the fail-open behavior.

### Signals-layer intent backend — SHIPPED (#142)

`SIGNAL_INTENT_BACKEND=reranker` is now **the default** (`cosine` opts out).
The same `FragmentScorer` is reused with an intent-framed instruct to score
phase exit-gate utterances against per-intent task descriptions, replacing
cosine similarity for the named-intent predicates. It earned the default by
passing its pre-registered gate on the labeled intent benchmark (macro-F1
0.242 → 0.687, negation-slice 0.85 → 1.00). Cosine remains the fail-open floor:
an unreachable reranker server or an explicit `cosine` opt-out degrades to it
byte-for-byte. (This was "out of scope (v1)" in the original sketch; it shipped,
then became the default once measured.)

## Runtime & ops

- **Model: `qwen3-reranker-0.6b`** — pair-scored via `/v1/completions`
  yes/no logprobs. (The earlier LFM2.5-350M vs `qwen3.5:0.8b` bake-off is
  obsolete; the reranker won.) Default served at `http://127.0.0.1:47952`.
- Budget: hard 600 ms timeout (per-request reaped at 0.9x the batch budget),
  then fail-open. Stage B scores up to `LM_ASSIST_MAX_CANDIDATES` (default 8)
  fragments concurrently (~150–250 ms). The reranker llama-server runs with
  `--parallel 8 -c 8192` so the fan-out lands as one wave.
- Config: `LM_ASSIST=off|arbitrate` (default `off`), `LM_ASSIST_MODEL`
  (default `qwen3-reranker-0.6b`), `LM_ASSIST_RERANK_URL`,
  `LM_ASSIST_TIMEOUT_MS`, `LM_ASSIST_MAX_CANDIDATES` (default 8, ==
  `--parallel`), `LM_ASSIST_DOC_CAP_CHARS` (default 2400),
  `LM_ASSIST_KEEP_THRESHOLD` (inert/gated-off default **0.0** — measure-then-set,
  no preset sets it). Signals backend: `SIGNAL_INTENT_BACKEND=cosine|reranker`,
  `SIGNAL_INTENT_RERANK_THRESHOLD`. Telemetry records the stage outcome
  (`hit|timeout|error|disabled`) per composition, and `/health` surfaces a
  `reranker` dependency (degraded when the recent window is timeout-dominant).

## How we know it works

The eval harness is the proof apparatus (see `BENCHMARKS.md`). The stages were
measured separately on 2026-06-12, and their shipped defaults follow the results:

1. **Fragment re-rank (`composed-lm`, `LM_ASSIST=arbitrate`) — measured, no lift
   → stays default-off.** On the LFM domain leg (n=5/task) composed-lm scored
   **0.827** in its as-shipped config, exactly tying the deterministic Stage-0
   baseline (0.827), and *regressed* to 0.809–0.817 once the candidate pool was
   widened (top-12 / four-channel runs). Its pre-registered gate was "composed-lm
   must beat composed and recover the domain_1 / domain_4 canaries"; **domain_1
   did not recover** (0.76, dropping to 0.48 in the top-12 variant). Gate not met
   → Stage B stays off by default. The code ships and is fail-open; it simply
   didn't earn the default.
2. **Graph expansion (`RETRIEVAL_GRAPH_EXPAND=on`) — measured, no lift → stays
   default-off.** The deterministic `det-edges` run tied the baseline (0.827).
3. **Signals intent backend — measured win → ships default-on.** The labeled
   intent benchmark lifts per-intent macro-F1 from 0.242 (cosine @ 0.75) to
   0.687 (reranker + negation guard) and negation-slice accuracy from 0.85 to
   1.00, at lower p50 latency. Gate passed → `SIGNAL_INTENT_BACKEND` defaults to
   `reranker`, with cosine as the opt-out and fail-open floor.
4. Gate: `eval/check_corpus_regression.py` unchanged with the LM stages off (the
   deterministic default path is unaffected).
5. Cost gate: p50/p95 compose latency reported both ways.

> **Deployment note.** Default-on `reranker` only delivers the measured lift
> where a `qwen3-reranker-0.6b` server is reachable (default `:47952`). The setup
> wizard now provisions that server (the installer writes embed (47951) +
> reranker (47952) llama-server units/plists — see `install/subcommands/enable_service.py`),
> so a fresh install serves the reranker by default; it still fails open to cosine
> if the server is unreachable.

## Risks

- **Determinism optics.** Mitigated by default-off, fail-open, and a
  deterministic default path. The claim is "deterministic by default;
  optional off-by-default LM-assist."
- **A second model to version.** The reranker tag is pinned and recorded
  in telemetry like the embed model.
- **Latency creep.** Hard timeouts; per-stage flags.
