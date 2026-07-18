---
phase: build
task_slug: env-fwd-run-command
route: full
domain_tags: [container, install]
scope:
  touches:
    - src/agentalloy/install/subcommands/container_runtime.py  # _run_container: merge forwarded_env() over baked dict
    - tests/test_container_e2e.py                              # deploy-seam assertions on the GENERATED command
  avoids:
    - container/entrypoint.sh + _build_entrypoint_script       # byte-identical drift guard stays green
    - upgrade.py / doctor.py                                    # T3/T4 contracts
success_criteria:
    - Generated argv carries -e for every intent key present in the host .env (incl. the assist-stack group); host-topology keys in .env are NEVER forwarded (baked /app/data paths intact); no .env → baked-only regression pin.
    - Forwarded URL_CLASS_UPSTREAM_KEYS value with a loopback host (localhost/127.0.0.1) → key still forwarded verbatim + exactly one warning line naming host.containers.internal; no warning for non-loopback upstreams or loopback rerank URLs.
    - _recreate_container delegates to _run_container (call-through pinned) — one renderer for setup and upgrade (spec AC 3).
    - Container e2e via the generated command: /health modules reflect the .env toggles; CODE_INDEX_WATCH observable via watch status (spec AC 1, 8, 12).
    - tests/test_container_edge_cases.py unchanged-green (spec AC 9).
related_contracts:
    - .agentalloy/contracts/build/env-fwd-registry.md
    - .agentalloy/contracts/design/container-module-env-propagation.md
created_at: 2026-07-06T19:53:43Z
---

# env-fwd-run-command

T2: wire forwarded_env() into _run_container and cover the deploy seam with
tests that assert on the command the PRODUCT generates — the seam CI never
exercised (the nightly hand-sets -e itself, which masked the shipped bug).
