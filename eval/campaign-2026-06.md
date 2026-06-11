# Rerun campaign — post retrieval-hardening (planned 2026-06)

Full-stack rerun across 4 models. Pre-register any new domain tasks and the
task→external-skill mapping BEFORE the first leg runs.

## Run matrix

| Task set | Tasks | Seeds | Conditions |
|----------|-------|-------|------------|
| generic  | 10    | 5     | none, composed, external |
| domain   | 16–20 (expanded from 8) | 5 | none, composed, flat (oracle), external |

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

- [ ] Expand `eval/domain_tasks.py` to 16–20 tasks covering packs the
      current 8 don't touch; pre-register graders with them.
- [ ] **Grader synonym audit** — graders regex for conventions as phrased
      in our packs; external skills may teach the same concept in
      different vocabulary. Audit every grader to accept synonymous
      correct answers, or the external arm is structurally rigged and a
      reviewer will spot it. Cross-check with the offline LLM-judge pass
      over `run-N.txt` outputs on any task where external scores oddly low.
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

## Interpretation guide (write down before seeing numbers)

- external ≈ flat-gold on domain → content is commodity; composition wins
  on tokens/speed alone.
- external < composed on domain → headline: composed beats installing a
  popular pack on both quality and cost.
- external < none on generic → context-swamping evidence: irrelevant
  static packs actively hurt, composed stays at baseline.
- Sparse-vs-dense split from the 2026-06-10 run is a hypothesis this
  campaign confirms or kills — not a prior finding.
