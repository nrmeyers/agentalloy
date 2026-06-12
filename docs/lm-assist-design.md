# LM-assist: a sub-1B intent layer riding alongside the embedder

Status: design sketch for review — 2026-06-12. Not scheduled.

## Why now

The 2026-06 campaign diagnostics surfaced three failures that are the same
failure: retrieval gets no help understanding *intent*.

1. **Rank-1 sensitivity.** The fragment-selection depth guarantee
   (`skill_granular_select`) pays large when fusion ranks the right skill
   first (7 of 18 domain tasks improved ≥0.15) and doubles down when it
   doesn't (`domain_4`: gold ranked #2 behind `webhooks-documentation`,
   composed fell 0.56 → 0.36). Fusion score margins are too noisy to
   gate depth on alone.
2. **The framework-ambiguity gap.** "I want to build a website that is a
   blog" retrieves `writing-readmes` / `ui-design-accessibility` /
   `brainstorming` — defensible, useless. The corpus has nextjs/vue/react
   packs; nothing bridges from *unstated* intent to stack vocabulary.
   Lexical collisions (web → webhooks) are already handled by the dense
   leg + RRF; *underspecified* queries are not.
3. **Signal-layer brittleness.** Phase exit gates are deterministic
   predicates plus cosine-similarity intents — the most threshold-tuned,
   least explainable code in the repo. Classification is what small
   instruct models are for.

The deprecated `sys-intake-*` spec docs (§6.3, "Routing via Qwen,"
confidence thresholds 0.6/0.4) already describe this architecture. This
doc narrows it to the smallest testable slice.

## Non-negotiable constraint

**"No LLM in the runtime path" stays true by default.** The deterministic
pipeline remains the product; LM-assist is an *optional, fail-open
enhancement stage*. On any LM timeout, error, or disabled flag, compose
behaves byte-for-byte as today. Layer-4 idempotency is asserted against
the default path; the LM path gets its own reproducibility story
(temp 0, fixed seed, pinned model tag) but no contractual guarantee.

## Stage 0 — index the skill card (deterministic, do FIRST)

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
hit framework cards). **Measure Stage 0 alone before building any LM
stage** — the LM stages below must justify themselves against the
post-Stage-0 baseline, not against today's.

## LM architecture

Two insertion points, independently flaggable:

### Stage A — query enrichment (pre-retrieval)

```
task ──> [LM: extract signals] ──> enriched query ──> embed + BM25 ──> RRF
   └──────────────── timeout/error: raw task ────────────────┘
```

One prompt, structured output, ~50 output tokens:

```json
{
  "domains": ["nextjs", "static-site", "markdown"],   // inferred stack/domain vocabulary
  "intent": "greenfield-build",                        // closed enum
  "phase_hint": "design"                               // closed enum, advisory only
}
```

`domains` terms are appended to the BM25 query and offered as soft
`domain_tags` (boost, not filter — wrong guesses must not exclude gold
skills). The raw task still drives the dense leg unchanged.

### Stage B — top-k arbitration (post-retrieval, pre-depth)

The score-conditional-depth problem, solved with a question instead of a
threshold: show the LM the task plus the top-5 skill names/summaries from
fusion; it answers `{"best": "<skill_id>", "confidence": 0..1}`.

- `best` agrees with fusion rank-1 → grant depth (current behavior).
- `best` disagrees with high confidence → promote it to rank-1, then depth.
- low confidence → fall back to pure breadth round-robin (pre-fix behavior).

This directly addresses domain_4 and bounds the blast radius of every
future rank-1 error.

### Explicitly out of scope (v1)

Phase-gate replacement (signal layer). Highest disruption, hardest to
benchmark with the existing harness. Revisit only if Stages A/B earn
their latency.

## Runtime & ops

- Model candidate (user-selected): **LFM2.5-350M** — ~350 MB quantized,
  built for fast structured extraction. Comparator: `qwen3.5:0.8b` (already
  pulled, the authoring model). Bake-off via the harness (open question 3);
  the 350M earns the slot iff it parses reliably into the Stage A/B JSON.
  Served by the same Ollama now pinned to the RTX 3060 — zero contention
  with benchmark/agent models on the 3090.
- Budget: hard 300 ms timeout per stage, then fail-open. Both stages
  enabled worst-case adds ~400–600 ms to compose; per-turn hook callers
  should enable Stage B only (one call, ~150–250 ms).
- Config: `LM_ASSIST=off|enrich|arbitrate|full` (default `off`),
  `LM_ASSIST_MODEL`, `LM_ASSIST_TIMEOUT_MS`. Telemetry records the stage
  outcome (`hit|timeout|error|disabled`) per composition.

## How we'll know it works

The harness from this campaign is the proof apparatus:

1. New run_poc condition **`composed-lm`** (same path as composed,
   `LM_ASSIST=full` service). Paired per-task deltas vs `composed`,
   same seeds.
2. **domain_4 is the canary** for Stage B: composed-lm must recover it
   toward its 0.96 oracle without regressing the 7 tasks the depth fix
   improved.
3. New Layer-1 probe task: framework-ambiguous web request ("build a blog
   website") with gold = {nextjs-or-vue pack skills}; Stage A must lift
   recall on it from 0/k.
4. Gate: `eval/check_corpus_regression.py` unchanged (LM-assist off) AND
   a new lm-assist variant run that must beat composed micro recall.
5. Cost gate: report p50/p95 compose latency both ways. If composed-lm
   doesn't beat composed by ≥0.05 mean domain score (outside our noise
   band) it doesn't ship — token savings alone don't justify the moving part.
6. Cheap iteration loop: one LFM domain leg (~45 min) per design change,
   exactly as practiced tonight.

## Risks

- **Determinism optics.** Mitigated by default-off, fail-open, and
  benchmarking the default path unchanged. The claim becomes
  "deterministic core; optional LM assist, measured."
- **Wrong enrichment poisons retrieval.** Mitigated: boosts not filters;
  dense leg always sees the raw task.
- **A second model to version.** Pin the tag, record it in the run
  manifest and telemetry like the embed model already is.
- **Latency creep.** Hard timeouts; per-stage flags; Stage B alone is the
  recommended per-turn-hook configuration.

## Open questions for review

0. Stage 0 shape: per-fragment header prefix (biases every fragment
   toward skill identity, changes all embeddings) vs synthetic card
   fragment (additive, but cards compete with content fragments for
   k slots) vs both?
1. Stage B prompt shape: skill names only, or names + one-line summaries?
   (Summaries cost tokens/latency, likely buy accuracy.)
2. Should `phase_hint` ever override the caller's phase? (Proposal: no —
   advisory telemetry only, until the signal-layer rework.)
3. Is 0.8B the right size, or do we benchmark 350M–1B variants the same
   way we benchmark agent models? (The harness makes this nearly free.)
4. Does Stage B subsume the planned score-conditional-depth heuristic, or
   do we want the deterministic heuristic as the fail-open behavior?
