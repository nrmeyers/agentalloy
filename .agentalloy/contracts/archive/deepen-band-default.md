---
phase: build
task_slug: deepen-band-default
route: fast
work_item: benchmark-fidelity-and-slot-leak
domain_tags: [retrieval]
scope:
  touches:
    - src/agentalloy/retrieval/domain.py       # _DEEPEN_BAND_DEFAULT 0.0 -> 0.85 + comments
    - tests/test_config_consistency.py         # default assertion flips; 0.0 = kill switch
    - .env.example                             # knob doc reflects active default
  avoids:
    - k defaults / DEFAULT_K_BY_PHASE          # k=4 stands (K sweep rejected k<=3 decisively)
    - selector logic                           # gate mechanics unchanged; only the default
success_criteria:
    - _DEEPEN_BAND_DEFAULT = 0.85; AGENTALLOY_DEEPEN_BAND=0.0 remains a byte-identical legacy kill switch.
    - Evidence pinned in comments - 2026-07-10 K sweep, LFM domain composed, paired seeds n=5, 0.8417@763 (band 0.85) vs 0.8156@776 (band 0.0); k=3/k=2 rejected (0.67-0.70); generic guard neutral (0.808 vs 0.805).
    - Version bump rides the PR (shipped-surface change).
related_contracts:
    - .agentalloy/contracts/build/retrieval-process-demotion-v2.md
created_at: 2026-07-10T17:40:00Z
---

# deepen-band-default

Flip the E4 deepen-gate default to the measured winner: spare selection slots
deepen the top (gold) skill instead of backfilling a 4th sibling. Small models
convert gold fragment depth to score, not breadth.
