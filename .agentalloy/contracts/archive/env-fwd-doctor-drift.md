---
phase: build
task_slug: env-fwd-doctor-drift
route: full
domain_tags: [install, cli]
scope:
  touches:
    - src/agentalloy/install/subcommands/doctor.py  # _check_module_drift in the container doctor path
    - tests/test_doctor.py                          # drift both directions + agreeing-silent
  avoids:
    - live re-sync of a running container            # apply-on-recreate only; doctor names the fix
    - container_runtime.py / upgrade.py
success_criteria:
    - .env toggle vs /health modules mismatch (either direction) → finding naming both sides and `agentalloy upgrade --recreate-only` (spec AC 7).
    - Agreeing state → check passes with no finding.
related_contracts:
    - .agentalloy/contracts/build/env-fwd-run-command.md
    - .agentalloy/contracts/design/container-module-env-propagation.md
created_at: 2026-07-06T19:53:43Z
---

# env-fwd-doctor-drift

T4: cover the apply-on-recreate window — after a .env edit the running
container is stale until recreated; doctor makes the drift visible with the
exact fix command.
