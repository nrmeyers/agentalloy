---
phase: design
task_slug: container-module-env-propagation
route: full
domain_tags: [container, install]
scope:
  touches:
    - src/agentalloy/install/env_forwarding.py                 # NEW: classification registry + forwarded_env()
    - src/agentalloy/install/subcommands/container_runtime.py  # _run_container merges forwarded intent keys over baked env
    - src/agentalloy/install/subcommands/upgrade.py            # post-recreate new-module notice
    - src/agentalloy/install/subcommands/doctor.py             # _check_module_drift (.env vs /health modules)
    - src/agentalloy/config.py                                  # audit anchor only (comments); no field changes
    - tests/                                                    # audit-enforcement + deploy-seam + notice + drift tests
  avoids:
    - container/entrypoint.sh                                   # byte-identical drift guard intact
    - Containerfile                                             # no image change
    - native deploy path                                        # unchanged by construction (process-env reads)
    - wholesale --env-file                                      # allowlist-only by design
success_criteria:
    - Generated run command forwards exactly the intent keys present in the host .env; host-topology keys never forwarded (deploy-seam tests on generated argv).
    - Audit test enumerates Settings.model_fields and fails on any unclassified key; INTENT/HOST_TOPOLOGY sets disjoint.
    - Both launch paths render env via the single _run_container seam (call-through pinned by test).
    - Upgrade prints one non-interactive notice per module toggle absent from the .env; doctor flags .env-vs-/health module drift with the recreate command.
    - Entrypoint drift guard unchanged-green; version bump at ship per RELEASE.md §4.
related_contracts:
    - .agentalloy/contracts/spec/container-module-env-propagation.md
    - .agentalloy/contracts/intake/container-module-env-propagation.md
created_at: 2026-07-06T19:53:43Z
---

# container-module-env-propagation — design

## Design in a sentence

Fix container env propagation inside the existing single choke point
(`_run_container`) by merging an audited allowlist of intent keys read from
the host `.env` over the baked env dict — with a full Settings key-inventory
audit enforced by test, a post-recreate new-module notice in `upgrade`, and a
doctor drift check for the apply-on-recreate window.

## Artifacts

- docs/design/container-module-env-propagation/approach.md — mechanism,
  full key classification (intent vs host-topology, incl. non-Settings keys),
  decisions (apply-on-recreate, verbatim upstream URLs + localhost caveat,
  native-asymmetry note), risks.
- docs/design/container-module-env-propagation/tasks.md — T1 registry+audit,
  T2 renderer+deploy-seam tests, T3 upgrade notice, T4 doctor drift.
- docs/design/container-module-env-propagation/test-plan.md — 15 cases mapped
  to spec ACs.

## Build contracts

One per task: env-fwd-registry, env-fwd-run-command, env-fwd-upgrade-notice,
env-fwd-doctor-drift (`.agentalloy/contracts/build/`).
