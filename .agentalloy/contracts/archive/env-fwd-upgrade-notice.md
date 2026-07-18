---
phase: build
task_slug: env-fwd-upgrade-notice
route: full
domain_tags: [install, cli]
scope:
  touches:
    - src/agentalloy/install/subcommands/upgrade.py  # post-recreate MODULE_TOGGLES diff → one-line notice
    - tests/test_upgrade.py                          # notice present/absent cases
  avoids:
    - any interactive prompt                          # notice-only per spec
    - container_runtime.py / doctor.py
success_criteria:
    - For each MODULE_TOGGLES key absent from the user .env, upgrade prints exactly ONE line naming the module and the enable command; exit code unaffected (spec AC 6).
    - Toggle present (either value) → no notice.
related_contracts:
    - .agentalloy/contracts/build/env-fwd-run-command.md
    - .agentalloy/contracts/design/container-module-env-propagation.md
created_at: 2026-07-06T19:53:43Z
---

# env-fwd-upgrade-notice

T3: tell upgrading users when the new version ships a module their .env
predates — the UX layer of the original report (upgraded to v6.1.2, zero
mention of code indexing anywhere).
