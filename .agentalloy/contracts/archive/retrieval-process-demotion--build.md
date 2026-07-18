---
phase: build
task_slug: retrieval-process-demotion
route: full
domain_tags: [retrieval, python]
scope:
  touches:
    - src/agentalloy/reads/models.py           # ActiveFragment.category_scope
    - src/agentalloy/reads/active.py           # _FRAGMENT_COLS projects s.category_scope
    - src/agentalloy/retrieval/domain.py       # demote_process_skills() + call sites (main + BM25 fallback)
    - tests/                                   # transform units + classification audit + plumbing test
  avoids:
    - src/agentalloy/_packs/                   # category_scope used as-authored, no pack edits
    - k defaults / DEFAULT_K_BY_PHASE          # out of scope
success_criteria:
    - demote_process_skills(fragments, k) pure function; fires only when >=1 non-process skill is within the top-W skill-lead window (W default 2*k); moves all process-scope fragments to the tail preserving relative order and returns the demoted skill_ids; no-op otherwise.
    - skill_granular_select folds demoted_skill_ids into its FAR last-resort tier (slots only after NEAR siblings drained + top skill deepened); empty/None set is byte-identical to legacy.
    - Process-scope = category_scope contains "process", read from hydrated fragment metadata; ActiveFragment/_FRAGMENT_COLS plumbed with a safe default for pre-existing corpora.
    - Applied before scores_by_id assignment on BOTH the main path and the BM25 circuit-breaker fallback path, so diversity-on and diversity-off selection both see the demoted order.
    - AGENTALLOY_PROCESS_DEMOTION (default on, off = byte-identical no-op) and AGENTALLOY_PROCESS_DEMOTION_WINDOW env knobs, following domain.py's existing env conventions.
    - Classification audit test pins the known generic set as process-scope and sampled framework skills (fastapi/react/snowflake) as not; fails on corpus drift.
related_contracts:
    - .agentalloy/contracts/design/benchmark-fidelity-and-slot-leak.md
created_at: 2026-07-08T02:10:00Z
---

# retrieval-process-demotion

T1 of docs/design/benchmark-fidelity-and-slot-leak/tasks.md: the windowed
process-class demotion mechanism and its metadata plumbing. Deterministic
Stage-0 only — no LM, no pack edits, no selector surgery. AC1.2/AC1.3 are
measured later in the acceptance run (T5), not here.
