# LM-assist: a sub-1B intent layer riding alongside the embedder

Status: design sketch for review — 2026-06-12. Revised same day after the
authored-external-skills experiment (see "What the experiment changed").
Stage 0 approved for implementation.

## Why now

The 2026-06 campaign diagnostics surfaced four failures that are the same
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
4. **Within-skill fragment selection.** `domain_1` (webhook signature):
   the gold skill ranks #1 and receives both depth slots, yet composed
   scores 0.60 where flat-oracle (whole skill) scores 1.00. The depth
   picker selects the intro fragments (f1/f5) because they share the
   task's surface vocabulary; the fragments the grader needs —
   `signed_content` construction and timing-safe compare (f9/f10) — use
   words the task never says. Skill-level ranking cannot fix this; only
   fragment-level relevance can.

## What the 2026-06-12 experiment changed

To split "content vs ranking" we authored the third-party Clerk/OpenAI
webhook skills through the authoring pipeline, ingested them, and reran
composed on domain_1/2/4 (n=5, 27B + LFM). Results were **bit-identical
to v2** (same input tokens, same scores): the new skills ranked 5th–6th
and won zero k=4 slots. Tracing domain_1 produced failure #4 above.

Consequences for this design:

- **Stage B must arbitrate fragments, not skills.** A skill-level
  arbiter would have looked at domain_1's top-5, agreed with rank-1, and
  changed nothing. One fragment-level mechanism covers domain_4 (demote
  the wrong skill's fragments), domain_1 (promote f10 over f1), and the
  12B-redshift case (pick nothing).
- **"No LLM in the runtime path" is no longer a product constraint**
  (decision 2026-06-12): the measured ceiling of deterministic fusion —
  composed captures ~45% of oracle lift, with the remainder concentrated
  in judgment calls — isn't worth the marketing line. Determinism remains
  the *fail-open floor*, not the headline. Target shape:
  `vector + BM25 + LFM-tiny = compose`.

The deprecated `sys-intake-*` spec docs (§6.3, "Routing via Qwen,"
confidence thresholds 0.6/0.4) already describe this architecture. This
doc narrows it to the smallest testable slice.

## Constraint (revised 2026-06-12)

**Determinism is the fail-open floor, not the headline.** The LM stage
may run in the default path once it earns its keep (ship gate below), but
on any LM timeout, error, or disabled flag, compose degrades to today's
deterministic composition byte-for-byte. Layer-4 idempotency is asserted
against the fail-open path; the LM path gets its own reproducibility
story (temp 0, fixed seed, pinned model tag) but no contractual
guarantee. The claim becomes "deterministic baseline, LM-refined when
available."

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

### Stage B — fragment re-rank (post-retrieval, pre-assembly)

Revised 2026-06-12 from skill-level arbitration to fragment-level
re-ranking — the domain_1 trace showed a skill-level arbiter would agree
with rank-1 and change nothing.

Show the LM the task plus the top ~12 fragments from fusion (skill name +
fragment type + first lines each); it answers
`{"keep": ["<frag_id>", ...], "confidence": 0..1}` selecting up to k.

- High confidence → assemble exactly the kept fragments, in fusion order.
- Low confidence, timeout, or unparseable → today's deterministic
  depth+round-robin selection (fail-open floor).
- `keep: []` with high confidence is **valid and means inject nothing** —
  the 12B-redshift case showed even oracle content hurts a model that
  already knows the domain.

One mechanism covers all three measured failures: domain_4 (wrong skill's
fragments demoted), domain_1 (f10 promoted over f1), redshift (empty
keep). It also subsumes the planned score-conditional-depth heuristic —
the deterministic selection remains as the fail-open behavior, answering
open question 4.

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
2. **domain_4 and domain_1 are the canaries** for Stage B: composed-lm
   must recover domain_4 toward its 0.96 oracle and domain_1 toward its
   1.00 flat-oracle (27B: composed 0.60 → the f9/f10 fragments must win
   slots) without regressing the 7 tasks the depth fix improved.
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
1. Stage B prompt shape: how much of each candidate fragment does the
   re-ranker see? (First lines are cheap; full content for 12 fragments
   may blow the 350M's useful context.)
2. Should `phase_hint` ever override the caller's phase? (Proposal: no —
   advisory telemetry only, until the signal-layer rework.)
3. Is 0.8B the right size, or do we benchmark 350M–1B variants the same
   way we benchmark agent models? (The harness makes this nearly free.)
4. ~~Does Stage B subsume score-conditional-depth?~~ Answered 2026-06-12:
   yes — deterministic selection is the fail-open behavior.
