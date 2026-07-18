---
phase: spec
task_slug: benchmark-fidelity-and-slot-leak
route: full
domain_tags: [retrieval, eval]
scope:
  touches:
    - src/agentalloy/orchestration/           # free-text domain-leg slot allocation mechanism (design chooses)
    - src/agentalloy/api/proxy_apply.py       # _merge_compose_telemetry: carry contract_path/contract_tags
    - src/agentalloy/telemetry.py             # ProxyComposeTelemetry fields if needed for provenance
    - eval/run_poc.py                         # legs=domain, composed-contract arm, manifest provenance, source_skills, preflight
    - eval/                                   # paired-run comparator; per-task contract fixtures
  avoids:
    - k value changes                          # K sweep is follow-up, after slot economics change
    - src/agentalloy/_packs/                   # no corpus/prose edits ride this item
    - LM_ASSIST / Stage B config               # posture unchanged (off)
    - container/entrypoint.sh                  # untouched; no image mechanics here
success_criteria:
    - AC1: gold retrieved 18/18 at k=2; domain composed >= 0.81 at <= 700 tok (strip-sim floor); generic set within +-0.02; deterministic Stage-0 mechanism.
    - AC2: composed arm legs=domain; composed-contract arm via compose_request_from_contract; manifest provenance (version, corpus stamp, effort, backend); gold preflight (present + non-deprecated); source_skills in meta; paired comparator.
    - AC3: contract_path/contract_tags populated on Tier-2 contract-scoped proxy rows, null on free-text rows; hermetic e2e through the real apply path.
related_contracts:
    - .agentalloy/contracts/intake/benchmark-fidelity-and-slot-leak.md
created_at: 2026-07-08T01:25:00Z
---

# benchmark-fidelity-and-slot-leak

## Scope in a sentence

Stop free-text domain retrieval from spending slots on generic quality skills
(measured floor: 0.812 @ 615 tok vs today's 0.789 @ 820), make Layer 2 measure
the shipped shapes (legs=domain composed arm + a contract-scoped arm) with
reproducible provenance and a gold-skill preflight, and populate contract
provenance on proxy telemetry rows — **not** k retuning, corpus edits, Stage B
changes, or the full proxy-surface session benchmark.

## Spec

Acceptance criteria and out-of-scope live in
docs/spec/benchmark-fidelity-and-slot-leak.md.
