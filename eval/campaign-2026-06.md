# Rerun campaign — post retrieval-hardening (planned 2026-06)

Full-stack rerun across 4 models. Pre-register any new domain tasks and the
task→external-skill mapping BEFORE the first leg runs.

## Run matrix

| Task set | Tasks | Seeds | Conditions |
|----------|-------|-------|------------|
| generic  | 10    | 5     | none, composed, external |
| domain   | 18 (expanded from 8) | 5 | none, composed, flat (oracle), external (6/18 mapped) |

Flat-gold (oracle) is **dropped from generic** — the oracle framing only
means something on domain tasks; saves ~200 calls. Generic's job is the
no-harm control and the token-discipline finding.

Total: 4 models × (10×3 + ~20×4) × 5 ≈ 2,200 calls, sequential,
single-GPU host. Run the 27B leg overnight.

## Conditions

- **none** — bare system prompt.
- **composed** — `/compose` per task, `k=4`.
- **flat (oracle)** — task's gold skills, full prose (domain only).
- **external** — implemented in `run_poc.py` (`--conditions ... external`);
  registry/mapping in `eval/external_skills.py`:
  - Domain tasks: the mapped third-party skill's full SKILL.md prose,
    injected verbatim and unmodified.
  - Generic tasks: a single fixed external bundle for every task
    (context-swamping test — realistic static-pack wiring).
  - Mapping lives in `eval/external_skills.py`: task_id → {skill name,
    source URL, commit SHA, license, local path under `eval/external/`}.
    Frozen into `manifest.json` (`external_skills` key) per run; the leg
    refuses to start until every selected task resolves to a file on disk.
  - Sourcing: popular, actually-installable skills (anthropics/skills,
    Vercel agent skills, high-star community SKILL.md repos). Record
    license per skill. Benchmark use only — never product corpus.

## Pre-flight checklist

- [x] Expand `eval/domain_tasks.py` to 16–20 tasks (2026-06-11: now 18).
      New tasks 9–18 target packs the original 8 didn't touch: temporal
      activities (timeouts/heartbeat/blocking), GH Actions concurrency +
      caching, redis streams + WATCH transactions, snowflake time travel +
      warehouse cost, otel trace propagation, airflow task hygiene,
      redshift table design. Graders pre-registered with the tasks and
      written synonym-aware from the start (strict only where the task
      asks to *name* an API). Winnability verified: every grader scores
      ≥4/4 against its own gold skill's prose (oracle arm can win).
      NOTE: tasks 9–18 have no external-skill mapping yet — the external
      arm covers 6/18 domain tasks until a new sourcing pass maps them
      (or documents that no genuine artifact exists).
- [x] **Grader synonym audit** (2026-06-11) — cross-checked every mapped
      domain grader against the external skill's actual vocabulary:
      - d1: accept `signed_content` (Clerk's spelling), bounded-freshness
        synonyms for the 5-min tolerance, timing-safe/secure-compare for
        constant-time.
      - d2: `mentions_redis_for_deduplication` → `mentions_dedup_store`
        (any concrete store counts; redis was our pack's phrasing);
        `mentions_24h_ttl` → `mentions_bounded_ttl` (any TTL/expiry).
      - d5: accept "move side effects into an activity" — Temporal's own
        skill teaches the activity fix, not `workflow.now()`.
      - d3/d6: external vocabulary already matched; no change.
      - d7: kept strict deliberately — the task explicitly asks to *name*
        `is_incremental()` and `unique_key`. Note the dbt external skill
        contains zero incremental coverage (verified); expect
        external ≈ none there and report it as a property of the pack.
      - d4/d8: unmapped (no external arm); left as pre-registered.
      - Generic graders: no audit needed — the external bundle is
        deliberately off-topic (swamping arm), so no vocabulary coupling.
      Loosenings only ever *remove* composed/flat advantage; they apply
      identically to every arm. Cross-check with the offline LLM-judge
      pass on any task where external still scores oddly low.
- [x] Implement `external` condition in `run_poc.py` (same injection path
      as flat, content from the `eval/external_skills.py` registry).
- [x] Source external skills into `eval/external/` and populate REGISTRY /
      TASK_MAPPING / GENERIC_BUNDLE. All entries are genuine third-party
      artifacts, copied verbatim with pinned commit SHAs + licenses
      (hookdeck/webhook-skills, temporalio/skill-temporal-developer,
      SpillwaveSolutions/mastering-gcloud-commands, dbt-labs/dbt-agent-skills,
      anthropics/skills). 6 of 8 domains mapped; domains 4 (webhook
      versioning) and 8 (SCD Type 2) have NO genuine public artifact and are
      deliberately unmapped — the harness skips external for those tasks and
      records `external_skipped_tasks` in the manifest. Coverage notes per
      entry live in `eval/external_skills.py` (several are honest partial
      matches — report that in the writeup, it's a realistic property of the
      incumbent practice, not a flaw).
- [ ] Report prompt sizes per condition — external SKILL.mds can be
      multi-KB; the token gap is part of the result.
- [ ] Report bootstrap CIs per cell and paired per-task deltas
      (composed vs each other arm, same task+seed).
- [x] LLM-judge harness (`eval/judge.py`): blind, length-controlled rubric
      (correctness/coverage/precision, 0–5 each), Claude Opus 4.8 via the
      Batches API with structured outputs. Reports paired deltas with
      bootstrap CIs, judge–heuristic agreement (Pearson r), and a
      length-bias diagnostic. Runs offline over persisted `run-N.txt`
      outputs — judge any time after the legs finish.
- [ ] Pin all agent-model legs to the RTX 3090 (`CUDA_VISIBLE_DEVICES=1`
      on the llama-server); 27B/35B quants spill to CPU on the 3060 and
      corrupt tok/s + wall-clock. Embed model can live on the 3060.

## Leg commands

```bash
# generic (no oracle arm)
AGENT_MODEL=<id> LM_STUDIO_URL=<url> \
  uv run python -m eval.run_poc --n 5 --label generic \
  --conditions none composed external

# domain (all four arms)
AGENT_MODEL=<id> LM_STUDIO_URL=<url> \
  uv run python -m eval.run_poc --n 5 --task-set domain --label domain \
  --conditions none composed flat external
```

## Judging (offline, after all legs)

```bash
export ANTHROPIC_API_KEY=...
uv run --with anthropic python -m eval.judge submit eval/runs/<leg1> <leg2> ...
uv run --with anthropic python -m eval.judge collect   # poll + write + report
uv run python -m eval.judge report eval/runs/<leg> ... # re-aggregate any time
```

Batch pricing is 50% off; ~2,400 judgments on Opus 4.8 ≈ $27. Trust the
judge only if judge–heuristic agreement is reasonable and the length-bias
diagnostic (|r| vs output tokens) stays low.

## Interpretation guide (write down before seeing numbers)

- external ≈ flat-gold on domain → content is commodity; composition wins
  on tokens/speed alone.
- external < composed on domain → headline: composed beats installing a
  popular pack on both quality and cost.
- external < none on generic → context-swamping evidence: irrelevant
  static packs actively hurt, composed stays at baseline.
- Sparse-vs-dense split from the 2026-06-10 run is a hypothesis this
  campaign confirms or kills — not a prior finding.
