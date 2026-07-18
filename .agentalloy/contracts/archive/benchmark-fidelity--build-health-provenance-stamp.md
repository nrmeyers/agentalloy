---
phase: build
task_slug: health-provenance-stamp
route: full
domain_tags: [api, python]
scope:
  touches:
    - src/agentalloy/api/health_router.py      # HealthResponse.service block (version + corpus_stamp)
    - src/agentalloy/storage/                  # active (skill_id, version_id) read for the stamp, if a helper is needed
    - tests/                                   # TestClient presence + stamp stability/sensitivity
  avoids:
    - eval/                                    # manifest consumption is T3 (eval-protocol-fidelity)
    - /diagnostics routes                      # unchanged; stamp lives on /health
    - pack source hashing                      # stamp is store-derived by design, not pack-file-derived
success_criteria:
    - GET /health carries service.version == agentalloy.__version__ and service.corpus_stamp (SHA-256 hex over sorted active (skill_id, version_id) pairs).
    - Stamp is deterministic across calls and row order; changes iff the active version set changes; cheap enough for per-run harness calls (single store read).
    - Health endpoint degrades gracefully when the store is unavailable (stamp null, health still responds per existing dependency semantics).
related_contracts:
    - .agentalloy/contracts/design/benchmark-fidelity-and-slot-leak.md
created_at: 2026-07-08T02:10:00Z
---

# health-provenance-stamp

T2 of docs/design/benchmark-fidelity-and-slot-leak/tasks.md: the provenance
source for benchmark manifests. Small additive API change; ships in the
wheel → participates in the single version bump at ship.
