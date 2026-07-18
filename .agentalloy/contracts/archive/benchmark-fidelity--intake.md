---
phase: intake
task_slug: benchmark-fidelity-and-slot-leak
route: full
domain_tags: [retrieval, eval]
scope:
  touches:
    - src/agentalloy/orchestration/          # free-text domain-leg slot allocation (generic-skill leak)
    - src/agentalloy/api/proxy_apply.py      # telemetry: contract provenance on proxy rows
    - eval/run_poc.py                        # Layer-2 protocol: legs, provenance, preflight, contract arm
  avoids:
    - k retuning                             # separate K sweep AFTER the leak fix changes the landscape
    - pack/corpus content edits              # no skill prose changes ride this item
    - Stage B / LM_ASSIST changes            # shipped-off posture unchanged
success_criteria:
    - Free-text composed no longer spends domain slots on generic quality skills when on-domain candidates exist (measured floor: strip-sim 0.812 @ 615 tok, domain set, LFM, k=4).
    - Layer-2 composed arm measures the shipped Tier-2 shape (legs=domain) and a contract-scoped arm measures the design->contract centerpiece.
    - proxy_request telemetry rows carry contract_path/contract_tags so production can be audited for contract-scoped vs free-text injection.
related_contracts: []
created_at: 2026-07-08T01:20:00Z
---

# benchmark-fidelity-and-slot-leak

## What the user actually wants

The 2026-07-07 LFM recampaign surfaced three interlocking defects: the Layer-2
benchmark measures a payload the product never repeatedly ships (bare /compose,
legs=both), free-text retrieval leaks generic quality skills into domain slots
(test-driven-development on 18/18 domain tasks; evicts gold at k=2), and proxy
telemetry drops contract provenance so none of this is auditable from
production data. Fix all three so benchmark numbers are trustworthy, free-text
injection stops paying a generic-prose tax small models can't afford, and the
contract mechanism's firing is observable.

## Routing

Full route. Three surfaces (retrieval orchestration, eval harness, proxy
telemetry) with a measured baseline and a pre-registered improvement floor —
spec pins acceptance, design decomposes into per-surface build contracts
(tag-focus: one surface each).

## Evidence base

Session analysis 2026-07-07: paired-seed probes (legs=domain 0.789; k=2 0.629
< none 0.663; strip-sim 0.812 @ −25% tokens), gold-hit 18/18, dogfood telemetry
audit (36 composed proxy rows, 0 with contract provenance), live
contract-scoped composes (tag-scoped, zero generic filler).
