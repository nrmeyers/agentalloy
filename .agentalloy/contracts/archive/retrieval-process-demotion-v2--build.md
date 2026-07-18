---
phase: build
task_slug: retrieval-process-demotion-v2
route: full
work_item: benchmark-fidelity-and-slot-leak
domain_tags: [retrieval, python]
scope:
  touches:
    - src/agentalloy/retrieval/domain.py       # about_exempt param + call-site evidence from bm25_hits; auto posture
    - tests/                                   # exemption units + posture matrix in test_config_consistency.py
  avoids:
    - src/agentalloy/_packs/                   # no pack/prose edits
    - k defaults / DEFAULT_K_BY_PHASE          # out of scope (deferred K sweep)
    - src/agentalloy/retrieval/lm_assist.py    # Stage B posture unchanged; auto only READS LM_ASSIST
    - selector (skill_granular_select)         # FAR-tier plumbing shipped in T1, reused as-is
success_criteria:
    - demote_process_skills(ranked, k, about_exempt) exempts skills in about_exempt from demotion AND from the FAR tier; about_exempt=frozenset() is byte-identical to v1.
    - Main call site computes about_exempt as the dense leg's process prefix (distinct-skill order up to the first non-process skill; parameterless). No dense leg (BM25-only fallback, empty bounded query) => demotion does not fire at all - the lexical leg is anti-signal for aboutness (measured 2026-07-10).
    - AGENTALLOY_PROCESS_DEMOTION gains "auto" (new default) = enabled iff LM_ASSIST != arbitrate; explicit on/off override; test_config_consistency.py pins the preset posture matrix (cpu effective-on, GPU presets effective-off).
    - Unit tests - about-shaped dense prefixes exempt; domain-shaped legs demote; prefix stops at the first non-process skill; unhydrated fragments skipped; v1 equivalence.
    - Probe guard with demotion forced on - paired retrieval_audit (on vs off, same corpus) net-positive with zero name-probe breaks; baselines met on a pristine packs-built corpus; eval.gold_hit --k 2 = 18/18; all recorded in the PR.
related_contracts:
    - .agentalloy/contracts/build/retrieval-process-demotion.md
    - .agentalloy/contracts/design/benchmark-fidelity-and-slot-leak.md
created_at: 2026-07-10T15:30:00Z
---

# retrieval-process-demotion-v2

T6 of docs/design/benchmark-fidelity-and-slot-leak/tasks.md: the aboutness
exemption that fixes v1's fatal confusion (query ABOUT a process skill vs
process filler on a domain task) using BM25-leg lead ranks as the
deterministic evidence, plus the LM-coupled `auto` posture so LM-less
deploys (CPU container) are protected by default while GPU keeps Stage B
arbitration primary. Deterministic Stage-0 only. AC1.2/AC1.3 are measured
by the acceptance protocol re-run after this lands, not here.
