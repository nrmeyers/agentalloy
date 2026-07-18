---
phase: build
task_slug: eval-contract-arm-and-comparator
route: full
domain_tags: [eval, python]
scope:
  touches:
    - eval/contracts/                          # NEW: 18 per-task contract fixtures (checked in)
    - eval/domain_tasks.py                     # task_id -> domain_tags map feeding the fixtures
    - eval/run_poc.py                          # composed-contract condition + summary wiring
    - eval/run_campaign.sh                     # domain leg gains composed-contract
    - eval/compare_runs.py                     # NEW: paired per-task comparator
    - tests/                                   # fixture-parse + payload + comparator units
  avoids:
    - src/agentalloy/                          # compose_request_from_contract imported as-is, never modified
    - .agentalloy/contracts/                   # fixtures are benchmark data, not repo workflow state
    - grader changes                           # scoring untouched
success_criteria:
    - eval/contracts/<task_id>.md fixture per domain task (frontmatter phase/task_slug/domain_tags from the new map, body = task spec); all 18 parse via parse_contract; a completeness test pins fixture set == DOMAIN_TASKS.
    - composed-contract condition builds its POST /compose payload via compose_request_from_contract(contract, legs="domain", k=k) — byte-equivalent to the proxy Tier-2 request shape — and is reported alongside composed/flat/none in summary.json.
    - eval/compare_runs.py prints per-task paired deltas per condition between two run dirs, pairing by task_id:condition:run_index (deterministic seeds).
related_contracts:
    - .agentalloy/contracts/design/benchmark-fidelity-and-slot-leak.md
created_at: 2026-07-08T02:10:00Z
---

# eval-contract-arm-and-comparator

T4 of docs/design/benchmark-fidelity-and-slot-leak/tasks.md: benchmark the
design→contract centerpiece for the first time, and make paired run
comparison a one-command operation. Lands after T3 (shares run_poc.py
seams). Not a shipped surface — no version bump.
