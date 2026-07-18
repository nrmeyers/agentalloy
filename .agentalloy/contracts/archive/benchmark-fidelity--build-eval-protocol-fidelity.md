---
phase: build
task_slug: eval-protocol-fidelity
route: full
domain_tags: [eval, python]
scope:
  touches:
    - eval/run_poc.py                          # legs=domain, manifest provenance, meta source_skills, preflight
    - eval/run_campaign.sh                     # no arm changes here; only if flags shift
    - tests/                                   # unit coverage for payload/manifest/preflight seams
  avoids:
    - src/agentalloy/                          # service is consumed, not changed (stamp ships in T2)
    - eval/tasks.py gold_skills values         # task definitions unchanged
    - contract fixtures / comparator           # T4 (eval-contract-arm-and-comparator)
success_criteria:
    - call_compose sends legs="domain"; manifest records legs plus service_version, corpus_stamp (from /health service block), AGENT_REASONING_EFFORT, and serving-backend identity from the agent endpoint's /v1/models.
    - run-N.meta.json persists source_skills for composed arms.
    - Preflight asserts every task's gold skills are present in the live corpus (/diagnostics/runtime store state) and not deprecated in pack source (expected_active_skill_ids); violation aborts naming task + skill before any model call.
    - No behavior change to none/flat/external arms; seeds and grading untouched.
related_contracts:
    - .agentalloy/contracts/design/benchmark-fidelity-and-slot-leak.md
created_at: 2026-07-08T02:10:00Z
---

# eval-protocol-fidelity

T3 of docs/design/benchmark-fidelity-and-slot-leak/tasks.md: make the
composed arm measure the shipped Tier-2 shape and make every run
reproducible from its manifest. Depends on T2 (health-provenance-stamp) for
the service block. eval/ is not a shipped surface — no version bump from
this task.
