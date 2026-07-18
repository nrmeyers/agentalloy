---
phase: spec
task_slug: container-module-env-propagation
route: full
domain_tags: [container, install]
scope:
  touches:
    - src/agentalloy/install/subcommands/container_runtime.py  # single shared run-command renderer; allowlist-forward intent keys from host .env
    - src/agentalloy/install/subcommands/upgrade.py            # recreate uses shared renderer; new-module notice
    - src/agentalloy/config.py                                  # key-inventory audit anchor: every Settings key classified intent vs host-topology
    - src/agentalloy/install/subcommands/doctor.py             # host-.env vs /health modules drift check (apply-on-recreate window)
    - tests/test_container_e2e.py                              # deploy-seam test: GENERATED run command carries exactly the intent keys
    - tests/test_container_edge_cases.py                       # stays green (entrypoint drift guard untouched)
  avoids:
    - container/entrypoint.sh                                   # env-driven by design; byte-identical drift guard intact
    - Containerfile                                             # image already ships [code-index] extra
    - native deploy path                                        # reads user .env directly; not broken (asymmetry noted for design)
    - wholesale --env-file forwarding                           # allowlist-only; host-topology keys never clobber baked container paths
success_criteria:
    - Fresh container setup selecting code-index/both yields /health modules.code_index=enabled with no manual flags; COMPOSE_ENABLED symmetric.
    - Host .env is the single source of truth; the renderer forwards intent keys through an audited allowlist; a test enumerates Settings fields and fails on any unclassified key.
    - First-run and upgrade-recreate render the run command from ONE shared function (test-asserted identical env); v6.1.x CODE_INDEX_ENABLED=1 backfills for free via the same read.
    - CODE_INDEX_WATCH forwards as an intent key; CODE_INDEX_DATA_DIR stays baked (host-topology).
    - Upgrade prints a one-line non-interactive notice when the new version ships a module toggle absent from the user .env.
    - Doctor flags host-.env vs running-container module drift with the exact recreate command; entrypoint drift guard unchanged-green; version bumped per RELEASE.md §4.
related_contracts:
    - .agentalloy/contracts/intake/container-module-env-propagation.md
created_at: 2026-07-06T19:29:21Z
---

# container-module-env-propagation

## Scope in a sentence

Make container deploys honor the user's `.env` by forwarding intent keys
(module toggles, CODE_INDEX_WATCH) through an audited allowlist in one shared
run-command renderer used by setup and upgrade — the audit classifies every
Settings key intent vs host-topology and a test enforces classification of
future keys — plus a one-line new-module upgrade notice and a doctor drift
check; **not** wholesale env forwarding, image/entrypoint changes, the native
path, or in-container data-dir overrides.

## Spec

Acceptance criteria and out-of-scope live in
docs/spec/container-module-env-propagation.md.
