---
phase: sdd-fast
task_slug: stage-b-default-posture
route: fast
domain_tags: [llama-server, python]
scope:
  touches:
    - src/agentalloy/install/presets/*.yaml        # LM_ASSIST=arbitrate + KEEP_THRESHOLD=0.05 + MAX_CANDIDATES>=16, all presets
    - src/agentalloy/retrieval/domain.py           # demotion default off; Stage B kept=0 -> deterministic fallback
    - src/agentalloy/install/env_forwarding.py     # forward LM_ASSIST_KEEP_THRESHOLD + AGENTALLOY_PROCESS_DEMOTION(_WINDOW)
    - tests/test_config_consistency.py             # posture guard flips from off to arbitrate
    - .env.example                                 # annotate the new keys
    - tests/                                       # fallback guard + preset consistency + forwarding coverage
  avoids:
    - container/entrypoint.sh beyond PR #368       # rerank launch args already fixed there; merge it as part of this item
    - k defaults / demotion algorithm changes      # mechanism unchanged; only defaults flip
    - corpus/pack edits                            # none
success_criteria:
    - All presets ship LM_ASSIST=arbitrate, LM_ASSIST_KEEP_THRESHOLD=0.05, LM_ASSIST_MAX_CANDIDATES>=16 (GPU keeps 24 if higher), per-hardware timeouts (CPU/apple 2000ms; GPU 600ms); test_config_consistency pins the NEW posture.
    - AGENTALLOY_PROCESS_DEMOTION defaults off (code default; knob stays as opt-in fallback for LM-less deploys; comments/docs carry no old-posture residue).
    - Stage B HIT with zero kept fragments falls back to the deterministic selection path instead of composing empty (telemetry outcome distinguishes fallback).
    - LM_ASSIST_KEEP_THRESHOLD, AGENTALLOY_PROCESS_DEMOTION, AGENTALLOY_PROCESS_DEMOTION_WINDOW join the env-forwarding INTENT_KEYS (audit test enumerates them).
    - PR #368 (container rerank --parallel 1 -c 2048) merged with this item.
    - QA: full suite + local corpus-regression chain green at shipped defaults; ship: version bump per RELEASE.md, container upgraded, /health shows arbitrate posture.
related_contracts: []
created_at: 2026-07-08T15:45:00Z
---

# stage-b-default-posture

Flip the shipped retrieval posture to the experimentally validated config:
Stage B LM arbitration (threshold 0.05, candidates >=16) becomes the
process-vs-domain discriminator; E7 windowed demotion becomes an opt-in
fallback (default off). Evidence (2026-07-08 session): demotion at any window
strands process skills (nightly 28931694381, probes 0.44-0.88 vs 0.93+ floors)
while Stage B at 0.05 scores 0.816 on the domain benchmark (crosses the 0.81
AC1.2 floor) AND lifts process-target reachability to build 0.962 / design
0.911 / qa 0.886 — at/above the no-mechanism ceiling. Reranker scores are
near-binary (filler exactly 0.0, relevant 0.86+), so 0.05 is a robust
separator. Includes the hardening trio surfaced by the same investigation:
container rerank launch args (#368), empty-keep fallback, env-forwarding
allowlist gaps.
