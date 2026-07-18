---
phase: build
task_slug: proxy-contract-provenance
route: full
domain_tags: [proxy, telemetry]
scope:
  touches:
    - src/agentalloy/api/proxy_apply.py        # ProxyComposeTelemetry.contract_path/contract_tags + merge carry from the Tier-2 request
    - src/agentalloy/api/proxy_telemetry.py    # write_proxy_trace gains the two params
    - src/agentalloy/api/proxy_router.py       # _write_flow_telemetry maps them through
    - tests/test_proxy_compose_telemetry.py    # merge unit
    - tests/                                   # hermetic e2e (contract-scoped + free-flow contrast)
  avoids:
    - src/agentalloy/storage/telemetry_store.py  # columns already exist; no schema change
    - orchestration record_trace semantics       # one consolidated proxy row stays the invariant
    - historical row backfill                    # explicitly out of scope
success_criteria:
    - _merge_compose_telemetry receives the Tier-2 domain request (or its contract fields) and populates ProxyComposeTelemetry.contract_path/contract_tags; absent request leaves None/[].
    - write_proxy_trace and _write_flow_telemetry carry the fields onto the composition_traces row (event_type=proxy_request).
    - Hermetic e2e through the real apply path (TestClient pattern of test_proxy_passthrough_native.py) with announce_cursor + seeded current_contract asserts the row's contract_path and contract_tags match the contract frontmatter; free-flow e2e asserts null/empty.
related_contracts:
    - .agentalloy/contracts/design/benchmark-fidelity-and-slot-leak.md
created_at: 2026-07-08T02:10:00Z
---

# proxy-contract-provenance

T5 of docs/design/benchmark-fidelity-and-slot-leak/tasks.md (code half):
close the two drop points so production rows are auditable for
contract-scoped vs free-text injection. The AC1.2/AC1.3 acceptance run rides
this task's tail once T1–T4 are landed, per the spec measurement protocol.
