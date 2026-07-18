---
phase: build
task_slug: ingest-secret-provisioning
route: full
domain_tags: [install-secret, container-bootstrap]
scope:
  touches:
    - src/agentalloy/install/                       # secret mint + persist + single resolver (native config)
    - src/agentalloy/install/subcommands/container_runtime.py  # _build_entrypoint_script — provision secret into agentalloy-data volume
    - container/entrypoint.sh                        # regenerated to match the generator (drift guard)
    - tests/test_ingest_secret.py                    # NEW
    - tests/test_container_edge_cases.py             # extend: entrypoint byte-identity
  avoids:
    - the endpoint handler and CLI routing   # T1/T2 CONSUME this resolver
    - inventing a second secret if one already fits   # check first
  success_criteria:
    - A random ingest secret is minted once and persisted to ~/.config/agentalloy/ with 0600 perms; a single resolver returns it for both the service (compare) and the CLI (send).
    - Container bootstrap provisions the SAME secret into the agentalloy-data volume so the in-container service and the host CLI converge on one value (AC-7).
    - A pre-existing secret is reused, never overwritten; fresh install provisions without manual steps.
    - "container/entrypoint.sh is byte-identical to container_runtime._build_entrypoint_script('') after the addition (drift guard test passes)."
    - Absent-secret behavior is defined and tested (generate-on-first-use OR fail-closed — pick and document).
related_contracts: [service-ingest-endpoint, service-mediated-cli-routing]
created_at: 2026-07-11T20:00:00Z
---

# ingest-secret-provisioning

Build T3 of `docs/design/service-mediated-corpus-ingest/`. The shared secret that
makes `/corpus/ingest-pack` authenticated (AC-7). Do this FIRST — T1 (compare) and
T2 (send) both depend on the resolver.

## Must
- Single resolver both sides import — no duplicated read logic.
- Native: mint → `~/.config/agentalloy/` (0600). Container: bootstrap writes the
  same value into `agentalloy-data` so host CLI and in-container service match.
- Reuse an existing secret; never overwrite.

## Watch
- **Check for an existing service-wide secret before minting a new one.** Do not
  add a second secret if one already fits the purpose.
- Container ↔ host convergence on one value is the whole point — a mismatch makes
  every container ingest a 401.
- **Entrypoint drift is a hard gate** (memory: container-entrypoint-drift-guard):
  edit `_build_entrypoint_script`, regenerate `container/entrypoint.sh`, and run
  `tests/test_container_edge_cases.py`. Never hand-edit the .sh.
- Perms: 0600 on the native file; the volume copy must not be world-readable in
  the image.
