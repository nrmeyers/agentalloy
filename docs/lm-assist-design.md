# LM-assist: a sub-1B intent layer riding alongside the embedder

Status: **shipped (flag-gated), v1.2.0.** Originally a design sketch
(2026-06-12); the slices it scoped are now in the codebase, all off by
default and fail-open to the deterministic path:

- **Stage 0 — skill-card indexing** shipped (the re-embed pass indexes a
  synthetic card per skill; `agentalloy reembed --card-index` defaults to
  `both` at the CLI).
- **Stage B — fragment re-ranker** shipped (#136) as `LM_ASSIST=arbitrate`,
  using `qwen3-reranker-0.6b`.
- **Signals-layer intent backend** shipped (#142) as
  `SIGNAL_INTENT_BACKEND=reranker`, reusing the Stage-B scorer.
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

AgentAlloy's composition path is deterministic by default. Two optional
LM-assist stages exist — a fragment re-ranker (`LM_ASSIST=arbitrate`) and a
signals-layer intent backend (`SIGNAL_INTENT_BACKEND=reranker`) — both off
by default, and both fail open to the deterministic path when the local
model is unavailable. On any LM timeout, error, or disabled flag, compose
degrades to deterministic composition byte-for-byte. Layer-4 idempotency is
asserted against the fail-open path; the LM path gets its own reproducibility
story (temp 0, pinned model tag) but no contractual guarantee. The absolute
"no LLM in the runtime path" claim was dropped 2026-06-12: deterministic
fusion captures ~45% of oracle lift, with the rest in judgment calls.

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

Mechanism: for the top ~12 fused fragments, the `qwen3-reranker-0.6b`
cross-encoder is shown the task plus one fragment at a time via
`/v1/completions` using the official Qwen3-Reranker chat template, asked
whether the Document meets the Query's requirements. It emits one token
with logprobs; `softmax(yes, no)` becomes the relevance score. Fragments
scoring above `LM_ASSIST_KEEP_THRESHOLD` are kept (capped at k, in fusion
order) and *replace* deterministic selection; everything else is dropped.
(llama.cpp's `/v1/rerank` endpoint skips the instruction template for this
GGUF and is not used.)

- HIT → assemble exactly the kept fragments, in fusion order.
- Disabled / timeout / error / empty-keep → deterministic depth+round-robin
  selection runs byte-for-byte as if Stage B never ran (fail-open floor).

This subsumes the score-conditional-depth heuristic: deterministic
selection is the fail-open behavior.

### Signals-layer intent backend — SHIPPED (#142)

`SIGNAL_INTENT_BACKEND=reranker` (default `cosine`). The same
`FragmentScorer` is reused with an intent-framed instruct to score phase
exit-gate utterances against per-intent task descriptions, replacing
cosine similarity for the named-intent predicates. Cosine remains the
fail-open floor. (This was "out of scope (v1)" in the original sketch; it
shipped.)

## Runtime & ops

- **Model: `qwen3-reranker-0.6b`** — pair-scored via `/v1/completions`
  yes/no logprobs. (The earlier LFM2.5-350M vs `qwen3.5:0.8b` bake-off is
  obsolete; the reranker won.) Default served at `http://127.0.0.1:60001`.
- Budget: hard 300 ms timeout, then fail-open. Stage B scores up to 12
  fragments concurrently (~150–250 ms).
- Config: `LM_ASSIST=off|arbitrate` (default `off`), `LM_ASSIST_MODEL`
  (default `qwen3-reranker-0.6b`), `LM_ASSIST_RERANK_URL`,
  `LM_ASSIST_TIMEOUT_MS`, `LM_ASSIST_KEEP_THRESHOLD`. Signals backend:
  `SIGNAL_INTENT_BACKEND=cosine|reranker`,
  `SIGNAL_INTENT_RERANK_THRESHOLD`. Telemetry records the stage outcome
  (`hit|timeout|error|disabled`) per composition.

## How we know it works

The eval harness is the proof apparatus (see `BENCHMARKS.md`):

1. Run condition **`composed-lm`** (`LM_ASSIST=arbitrate`), paired
   per-task deltas vs `composed`, same seeds.
2. **domain_4 and domain_1 are the Stage B canaries**: composed-lm must
   recover domain_4 toward its 0.96 oracle and domain_1's f9/f10 fragments
   must win slots, without regressing the depth-fix wins.
3. Signals backend: the labeled intent benchmark lifts per-intent macro-F1
   from ~0.24 (cosine @ 0.75) to ~0.72 (reranker + negation guard).
4. Gate: `eval/check_corpus_regression.py` unchanged with LM-assist off
   (the default path stays deterministic).
5. Cost gate: p50/p95 compose latency reported both ways.

## Risks

- **Determinism optics.** Mitigated by default-off, fail-open, and a
  deterministic default path. The claim is "deterministic by default;
  optional off-by-default LM-assist."
- **A second model to version.** The reranker tag is pinned and recorded
  in telemetry like the embed model.
- **Latency creep.** Hard timeouts; per-stage flags.
