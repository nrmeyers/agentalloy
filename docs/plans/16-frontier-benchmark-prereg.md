# Pre-registration: small model + AgentAlloy vs bare frontier APIs

**Status:** PRE-REGISTERED — locked before any arm runs. Deviations go in the
[Deviations log](#deviations-log) with a reason, never silently.

**Thesis under test:** a small, locally-served model with just-in-time composed
context is *non-inferior* to a bare frontier API model on domain coding tasks —
i.e. the product's "small models with context compete with enterprise API models"
claim, stated as a falsifiable hypothesis rather than a slogan.

**One-liner result shape we are trying to earn:** "Gemma 4 12B + AgentAlloy scores
within 0.03 of bare Sonnet on the 18-task domain suite, at $0 marginal cost per
task and no data leaving the box."

---

## 1. Hypotheses

We register a **non-inferiority** test, not a superiority test. The product wins if
the small stack *ties* the frontier model — matching quality at a fraction of the
cost and with local privacy is the whole thesis. Beating it is a bonus, not the
bar.

Let `Δ = score(challenger) − score(reference)` on the primary endpoint.

- **Primary H₁ (non-inferiority vs Sonnet):** the challenger is non-inferior to
  bare Sonnet if the lower bound of the 95% bootstrap CI on `Δ` exceeds `−δ`.
- **Secondary H₂ (non-inferiority vs Opus):** same test against bare Opus. Opus is
  the harder bar; we expect to *lose* non-inferiority here and will report the gap
  honestly as "X% of Opus at 0% of the cost."
- **Exploratory H₃ (generic tasks):** run the generic (non-domain) suite too. We
  **expect to lose** it — AgentAlloy's corpus is domain skills, and generic tasks
  are where composed context helps least. Registering the expected loss up front is
  the honesty control: a design that only reports its favorable slice is marketing.

**Non-inferiority margin: `δ = 0.03`** on the [0,1] deterministic-grader scale, the
same margin for every challenger model. Chosen before seeing API numbers, and
deliberately conservative. The band that actually matters is each challenger's own
domain seed spread on this suite — not a generic number. In v6.6.8 the domain scores
sit near ceiling (Gemma 4 12B composed 0.956; the 35B higher still), so their
across-seed spread is well under 0.01; `δ = 0.03` therefore sits comfortably above the
empirical "indistinguishable from zero" band for these cells. We confirm the per-model
per-task seed SD at analysis time and log it in the manifest; if any model's band
turns out larger than assumed, δ is revisited as a deviation, not silently.

---

## 2. Arms

All arms answer the *same* task specs and are graded by the *same* graders. Only
the system-prompt content and the serving endpoint differ.

Arms split into a **local challenger side** (three models, each run through the same
three local arms) and a **frontier reference side** (two bare API models, run once).

**Challenger models (local, free to run).** Each is served by its own llama-server
config and run through all three local arms below:

| Model | Model ID (pinned at run time) | Role |
|-------|-------------------------------|------|
| Gemma 4 12B IT | `gemma-4-12b-it` | the v6.6.8 baseline challenger; the headline "12B ties Sonnet" claim |
| Gemma 4 27B A4B | `gemma-4-27b-a4b` | mid MoE (≈4B active) — does more base capacity widen or close the gap under composition? |
| Qwen3.6-35B-A3B | `qwen3.6-35b-a3b` | strong MoE (≈3B active); near-oracle on domain in v6.6.8 — the local upper bound |

Model IDs are illustrative until the sibling session's llama configs land; the exact
served IDs (and GGUF hashes, §12) are pinned in the manifest before the run.

**Local arms** (applied to *every* challenger model):

| Arm | Serving | Context injected | Role |
|-----|---------|------------------|------|
| `challenger` | local llama-server (OpenAI-compat) | AgentAlloy `composed` (free-text, k=4) | the product stack |
| `challenger-contract` | local | AgentAlloy `composed-contract` (SDD path) | the shipped centerpiece mode |
| `local-floor` | local | none (bare) | isolates the AgentAlloy lift from the base model |

**Frontier reference arms** (run once, not per challenger model):

| Arm | Model | Serving | Context injected | Role |
|-----|-------|---------|------------------|------|
| `ref-sonnet` | `claude-sonnet-5` | Anthropic API | none (bare system prompt) | frontier reference |
| `ref-opus` | `claude-opus-4-8` | Anthropic API | none (bare system prompt) | harder frontier reference |

For Gemma 4 12B, `local-floor` and `challenger` reuse the exact arms already in the
v6.6.8 campaign (`none` and `composed`), so that model needs no new run — only the
27B and 35B challenger models and the two API reference arms are new work.

Each challenger model gets its own `local-floor → challenger` lift and its own
non-inferiority test against the shared frontier bar. We deliberately do **not** test
composed context on a frontier model here — the thesis is about closing the gap for
small models, and whether stuffing context also helps Sonnet is a separate question
we are not spending API budget on in this campaign.

---

## 3. Task suite

- **Domain suite (primary):** the 18 domain tasks in `eval/domain_tasks.py`, ×5
  seeds on local arms (deterministic seed = `sha256(task_id:condition:run_index)`
  per `run_poc.run_one`). API arms cannot honor seeds (see §6) so they run **N=5
  independent samples per task** at `temperature=0.2` and are treated as a sample,
  not a reproduction.
- **Generic suite (exploratory, H₃):** the generic task set, same structure. This
  runs **in the same campaign**, not as a conditional follow-up — deciding whether to
  run it after seeing the domain result would be optional-stopping. It is registered
  in and its expected loss (H₃) is frozen in §10.
- **Cell counts.** Local side = 3 challenger models × 3 local arms (`challenger`,
  `challenger-contract`, `local-floor`) × 18 tasks × 5 seeds = **810 local cells**
  per suite (Gemma 4 12B's 2 baseline arms already exist from v6.6.8, so ~750 are new).
  Local cells are **free** — they cost GPU wall-clock, not tokens. API side is
  unchanged by the extra local models: `ref-sonnet`, `ref-opus` = 2 × 18 × 5 =
  **180 API calls** for the domain suite (the sole token cost), ×2 for the generic
  suite = **360 API calls total**. At Sonnet/Opus list pricing this is the
  low-tens-of-dollars range; see §8. Note the frontier bar is shared across all three
  challenger models, so adding models multiplies only the free local side.

---

## 4. Endpoints (what we measure)

1. **Deterministic grader score** (primary) — the binary criteria in
   `eval/domain_tasks.py` / `eval/gold_hit.py`, de-brittled in #141 to credit
   synonyms/paraphrase. Mean over tasks×seeds, per arm.
2. **27B LLM-judge score** (co-primary — see §5) — pointwise rubric score from the
   local Qwen3.6-27B judge (`eval/judge_local.py`, `JUDGE_MODEL=qwen3.6-27b`),
   scoring the *same* `run-N.txt` artifacts against the shared rubric in
   `eval/judge_common.py`.
3. **Injected context tokens** (secondary) — prompt tokens attributable to skill
   injection, from the `usage.prompt_tokens` the harness already records.
4. **Cost per task** (secondary) — $0 marginal for local arms; API list price ×
   measured token usage for the frontier arms (§8).
5. **Wall-clock latency** (reported, not decisive) — `agent_ms` per cell.

---

## 5. Grader plan — deterministic *and* judge, both co-primary

The deterministic graders are cheap, reproducible, and leak-resistant, but they
have a **ceiling risk**: on near-solved tasks every arm scores ~0.99 and the graders
lose resolution — they cannot tell "correct" from "correct and idiomatic." A
non-inferiority test run only on a saturated grader would declare a tie by
construction.

Mitigation, registered up front:

- The **27B judge is co-primary, not a tiebreaker.** We judge **all five arms**
  (not just the small-model ones) so the frontier arms get no free pass and the
  judge's own bias is applied symmetrically.
- **Judge blind to arm:** artifacts are scored without the arm label in the prompt
  (the harness already scores raw `run-N.txt`); the judge sees output + task +
  rubric only.
- A tie must hold on **both** endpoints to be reported as a tie. If deterministic
  says tie and the judge says the frontier model is meaningfully better, we report
  the split — that *is* the finding (graders saturated, quality gap real).
- **Judge validation:** we already have pooled judge-vs-grader agreement from
  v6.6.8 (+0.084, CI [+0.056, +0.114], 540 judgments) establishing the judge tracks
  real quality; we cite it, and re-check agreement on this run's local arms.

The judge runs sequentially on one GPU slot at 30–60 s/judgment (thinking trace) —
a full pass is multi-hour and **must** checkpoint (`judge_local` appends every
verdict; resumable).

---

## 6. Determinism, drift, and confounds

The frontier arms break three invariants the local benchmark relies on. We name
each and its mitigation rather than pretending the comparison is apples-to-apples.

| Invariant (local) | Broken by API because | Mitigation |
|---|---|---|
| Deterministic seed reproduces a cell | Anthropic OpenAI-compat ignores `seed`; sampling is non-deterministic | Treat API cells as N=5 **samples**; report per-task variance; never claim reproduction |
| Model is frozen | Frontier model IDs are served versions that can change | **Pin exact IDs** `claude-sonnet-5` / `claude-opus-4-8` **+ capture the run date** in the manifest; a re-run under a different served build is a *new* experiment |
| Same decode settings across arms | API and llama-server differ in tokenizer, stop handling, `reasoning_effort` support | Hold `temperature=0.2`, `max_tokens=8192` on both; **omit `reasoning_effort`** on API arms (`AGENT_REASONING_EFFORT=""`) since the field is a local-template hint; document that decode parity is approximate |

Model pins and the run date go in the run manifest at execution time (scripts can't
call `Date.now()`-equivalents for us — the operator stamps it).

---

## 7. Plumbing — the run_poc patch

`eval/run_poc.py` already sends OpenAI `/v1/chat/completions` to `LM_STUDIO_URL`
with `AGENT_MODEL`. Two small, additive changes make it API-capable without
touching the local path:

1. **Auth header.** `call_agent`'s httpx client currently sends no `Authorization`.
   Add an optional bearer read from env (e.g. `AGENT_API_KEY`); when unset, behavior
   is byte-identical to today (local llama-server needs no key). For the Anthropic
   OpenAI-compat surface, point `LM_STUDIO_URL=https://api.anthropic.com` and set
   `AGENT_API_KEY=$ANTHROPIC_API_KEY`.
2. **Effort omission.** Already supported: `AGENT_REASONING_EFFORT=""` omits the
   field. Set it for the API arms.

Per-arm env matrix (illustrative):

```
# challenger / local-floor / challenger-contract  (existing local path)
# repeat per challenger model — point LM_STUDIO_URL at that model's llama-server,
# set AGENT_MODEL to its served ID:
#   gemma-4-12b-it | gemma-4-27b-a4b | qwen3.6-35b-a3b
LM_STUDIO_URL=http://<local>:<port>  AGENT_MODEL=<model-id>  AGENT_REASONING_EFFORT=none

# ref-sonnet
LM_STUDIO_URL=https://api.anthropic.com  AGENT_MODEL=claude-sonnet-5
AGENT_API_KEY=$ANTHROPIC_API_KEY         AGENT_REASONING_EFFORT=

# ref-opus
LM_STUDIO_URL=https://api.anthropic.com  AGENT_MODEL=claude-opus-4-8
AGENT_API_KEY=$ANTHROPIC_API_KEY         AGENT_REASONING_EFFORT=
```

If Anthropic's OpenAI-compat surface rejects any field the harness sends, the
fallback is a thin litellm proxy in front of the same `LM_STUDIO_URL` seam — no
harness change beyond the base URL. The auth-header patch is preferred (one arg,
no new dependency).

**Preflight guards to add before spending API budget:** assert `AGENT_API_KEY` is
present for API arms; assert the served model ID echoed in the response matches the
pinned ID; dry-run one task per API arm and eyeball the output before the full fan-out.

---

## 8. Cost axis

The second headline number, reported alongside quality:

- **Local arms:** $0 marginal per task (electricity aside); the model is already
  resident. This is the product's structural advantage.
- **API arms:** list price × measured `prompt_tokens` + `completion_tokens` per
  cell, summed. Report **cost per task** and **total campaign cost**.
- Headline framing when the frontier arm wins on quality but loses on cost:
  "ref-opus scores +0.0X over the local stack at ~$Y/task vs $0 — the local stack
  delivers Z% of Opus quality at 0% of the marginal cost, on-prem." Data
  transparency: if we lose quality, we say so with the number, then contextualize
  with cost — we do not bury the quality gap.

---

## 9. Statistical plan

- **Paired bootstrap. The resampling unit is the task — N = 18.** Each arm reduces
  to one score per task (the mean over its 5 seeds/samples, which only shrinks
  per-cell noise; the 5 does *not* set CI width). Resample the **18 paired task-means**
  with replacement, 10,000 iterations; for each, compute mean
  `Δ = score(challenger) − score(reference)`; take the 2.5/97.5 percentiles as the
  95% CI. All CI width in this design comes from the 18 tasks, not the seeds.
- **Power check before spending API budget.** From the v6.6.8 per-task domain scores
  we estimate the expected CI half-width at N=18 up front (per challenger model); if
  18 paired tasks cannot resolve `δ = 0.03` even under the null, that is known *before*
  the run and recorded here — we do not discover an underpowered design after paying
  for it. The frontier arms are the only paid side, so this check gates API spend.
- **Decision rule (per reference arm):**
  - CI lower bound `> −δ` → **non-inferiority holds** (the registered win).
  - CI lower bound `≤ −δ` and CI upper bound `< 0` → **inferior** (report the gap).
  - CI straddles 0 with lower bound `≤ −δ` → **inconclusive at N=18** (report; do not
    spin as a tie).
- Run the identical test on **both** the deterministic and judge endpoints; a
  reported tie requires both.
- No optional stopping: the 18 tasks and 5 seeds/samples are fixed in advance. If the
  N=18 CIs are too wide to decide, that is a *reported outcome* ("underpowered at
  N=18"), and any re-run at larger N is logged as a deviation.

---

## 10. Pre-registered predictions (so hindsight can't move them)

Written before the run, to be scored honestly afterward. Predictions 1–3 are stated
for the **Gemma 4 12B** headline challenger; the per-model predictions for the 27B and
35B challengers follow in prediction 5.

1. `challenger` (12B) is **non-inferior to `ref-sonnet`** on the domain suite (H₁ holds).
2. `challenger` (12B) is **inferior to `ref-opus`** on the domain suite (H₂ fails); gap
   ≤ ~0.05.
3. `challenger` (12B) **loses the generic suite** to both frontier arms (H₃).
4. Deterministic and judge endpoints **agree on the domain tie** but the judge
   widens the Opus gap (grader saturation on near-solved tasks).
5. Non-inferiority vs `ref-sonnet` is **monotone in base capacity**: if the 12B ties
   Sonnet, the 27B and 35B challengers do too, and the 35B additionally **closes the
   Opus gap** more than the 12B does (its v6.6.8 domain score already edges the local
   oracle).

A result that contradicts these is more interesting than one that confirms them;
either way the predictions are frozen here.

---

## 11. What would falsify the thesis

- The 12B `challenger` inferior to `ref-sonnet` beyond `−δ` on **both** endpoints →
  the "small + context competes with frontier" claim fails at 12B on this suite. (If
  the 12B fails but the 27B/35B tie, the claim survives only at larger local scale —
  reported as such, not spun.)
- The domain tie exists on deterministic graders but **collapses under the judge**
  → the win was a grader-saturation artifact, not real parity.

---

## 12. Runbook

```
# 0. patch: add AGENT_API_KEY bearer to call_agent (§7), land + test locally

# 1. local arms — one pass PER challenger model (free; sequential — one inference
#    at a time so llama-server doesn't spill layers to CPU). Gemma 4 12B's `none` +
#    `composed` already exist from v6.6.8; the 27B and 35B passes are new.
#    Bring up each model's llama-server, then point the harness at it:
for M in gemma-4-12b-it gemma-4-27b-a4b qwen3.6-35b-a3b; do
  # (start / confirm the llama-server for $M, set LM_STUDIO_URL to its port)
  uv run python -m eval.run_poc --conditions composed,composed-contract,none \
      --model "$M" --seeds 5 --out "eval/runs/<ts>-frontier/$M"
done

# 2. API arms (run ONCE — shared frontier bar for all three challenger models):
#    preflight one task each, verify model-ID echo + cost, THEN fan out.
#    ref-sonnet, ref-opus  (env per §7 matrix)

# 3. judge ALL arms across ALL models (co-primary), resumable, multi-hour
uv run python -m eval.judge_local run eval/runs/<ts>-frontier
uv run python -m eval.judge_local report eval/runs/<ts>-frontier

# 4. paired bootstrap PER challenger model vs the shared reference arms, + cost
#    rollup; write results into BENCHMARKS.md only after both endpoints are in and
#    the deviations log is reconciled.
```

Stamp the run manifest with: exact API model IDs, run date, Anthropic served-build
note, **per challenger model the GGUF filename + SHA-256 and the llama-server
version/commit** (each local model's "served build" — as un-reproducible across a
re-quant or engine bump as the API build is across an Anthropic re-serve, so pinned
the same way), δ, N, the measured per-model per-task seed SD (§1/§9), and the git SHA
of the harness + corpus.

---

## 13. Registered execution scope (minimal run)

The full design above (§§1–12) is the pre-registered *experiment*. This section
registers a cheaper *execution* of it — the API-side reductions below are locked
in advance so running the minimal version is not a post-hoc deviation.

**What ships in this run:**

- **Domain suite only on API arms.** The generic suite (H₃) is exploratory and its
  loss is already predicted (§10.3); we get it **free** by running it on the local
  arms only (all three challenger models × three local arms, as §3 already
  specifies) and skip it on the paid frontier side. H₃ is scored per challenger
  model against the *domain* frontier numbers, same as before — we just don't pay
  API twice.
- **3 samples per cell on API arms, not 5.** §9 already establishes that CI width
  is task-dominated (N=18 tasks), not sample-dominated — the 5→3 cut narrows the
  per-cell mean estimate slightly but does not change the bootstrap's resolving
  power. We still report the within-task sample SD (now over 3 draws) to keep the
  "lucky draw" check intact.
- **Both `ref-sonnet` and `ref-opus` run.** Primary H₁ (non-inferiority vs Sonnet)
  and secondary H₂ (vs Opus) both execute this pass — only the generic suite and
  the sample count are cut, not the reference models.

**Resulting API cost:** 18 domain tasks × 3 samples × 2 models (`ref-sonnet`,
`ref-opus`) = **108 API calls**, once, shared across all three challenger models —
down from the 360-call full design. Local side (810 cells) is unchanged and free.

**What this defers, explicitly:**

- The generic suite (H₃) on the API arms — scored only on local arms this pass
  (still free, still registered, just not paid twice).
- Nothing on H₁, H₂, or prediction 5 is deferred: both reference models run, so
  the Opus-gap-closing claim is fully testable this pass.

---

## Deviations log

_(empty — append every departure from this pre-registration with date + reason)_
