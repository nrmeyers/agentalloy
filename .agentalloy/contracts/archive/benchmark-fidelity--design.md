---
phase: design
task_slug: benchmark-fidelity-and-slot-leak
route: full
domain_tags: [retrieval, eval]
scope:
  touches:
    - src/agentalloy/reads/                    # category_scope onto ActiveFragment + _FRAGMENT_COLS
    - src/agentalloy/retrieval/domain.py       # windowed process-class demotion (main + BM25-fallback paths)
    - src/agentalloy/api/health_router.py      # /health service block: version + corpus_stamp
    - src/agentalloy/api/proxy_apply.py        # ProxyComposeTelemetry + merge carry contract provenance
    - src/agentalloy/api/proxy_telemetry.py    # write_proxy_trace contract_path/contract_tags params
    - src/agentalloy/api/proxy_router.py       # _write_flow_telemetry mapping
    - eval/                                    # legs=domain, contract arm + fixtures, preflight, provenance, comparator
    - tests/                                   # unit + classification audit + hermetic e2e
  avoids:
    - src/agentalloy/_packs/                   # no corpus/prose/metadata edits; category_scope used as-authored
    - k value changes                          # K sweep is follow-up after slot economics change
    - LM_ASSIST / Stage B config               # posture unchanged (off)
    - src/agentalloy/retrieval/domain.py::skill_granular_select  # selector untouched; demotion is pre-scoring
    - container/entrypoint.sh                  # no image mechanics
success_criteria:
    - Windowed process-class demotion (category_scope-based, W default 2k, env kill switch) reorders process skills to the tail only when a non-process skill is in the top-W window; generic pools are byte-identical no-ops; applied on main + BM25-fallback paths before scoring; deterministic pure function (AC1.4), gold-hit 18/18 at k=2 (AC1.1).
    - Layer-2 composed arm sends legs=domain; composed-contract arm builds payloads via compose_request_from_contract over checked-in eval/contracts/ fixtures; manifest records legs, service version, corpus stamp, effort, serving backend; gold preflight aborts on missing/deprecated; meta.json keeps source_skills; paired comparator (AC2.1-AC2.6).
    - /health exposes service.version + service.corpus_stamp (sorted active (skill_id, version_id) SHA-256), the provenance source for the manifest (AC2.3).
    - Tier-2 contract-scoped proxy rows populate contract_path/contract_tags via request-to-merge carry; free-text rows stay null; unit merge test + hermetic e2e through the real apply path (AC3.1-AC3.3).
    - Acceptance run per spec measurement protocol: domain composed >= 0.81 at <= 700 tok, generic within +-0.02, compared paired against the 2026-07-07 baselines.
related_contracts:
    - .agentalloy/contracts/spec/benchmark-fidelity-and-slot-leak.md
    - .agentalloy/contracts/intake/benchmark-fidelity-and-slot-leak.md
created_at: 2026-07-08T02:10:00Z
---

# benchmark-fidelity-and-slot-leak — design

## Design in a sentence

Demote process-scope skills to last-resort backfill in the free-text domain
leg via a windowed, kill-switchable pure reorder keyed on existing
`category_scope` metadata; make Layer 2 benchmark the shipped shapes
(legs=domain + a contract-scoped arm through `compose_request_from_contract`)
with reproducible provenance from a new `/health` service block; and carry
`contract_path`/`contract_tags` from the Tier-2 request into the consolidated
proxy telemetry row.

## Artifacts

- docs/design/benchmark-fidelity-and-slot-leak/approach.md — mechanism
  choice (demotion over exclusion/down-weight, with rationale), architecture
  flow, decisions (fixtures checked in, stamp semantics, version-bump line),
  risks (window mis-fire, gold-outside-window, scope drift).
- docs/design/benchmark-fidelity-and-slot-leak/tasks.md — T1 retrieval
  demotion, T2 health stamp, T3 eval protocol fidelity, T4 contract arm +
  comparator, T5 proxy provenance + acceptance run; sequencing (T3 after T2,
  acceptance last).
- docs/design/benchmark-fidelity-and-slot-leak/test-plan.md — 18 cases
  mapped to spec ACs.

## Build contracts

One per task (`.agentalloy/contracts/build/`): retrieval-process-demotion,
health-provenance-stamp, eval-protocol-fidelity,
eval-contract-arm-and-comparator, proxy-contract-provenance.
