---
phase: build
task_slug: 02-gate-wiring-and-escape-hatch
route: full
domain_tags:
  - install-pack
  - cli-subcommand
scope:
  touches:
    - "src/agentalloy/install/subcommands/install_pack.py"
    - "tests/**"
  avoids:
    - "src/agentalloy/api/**"
    - "src/agentalloy/orchestration/**"
    - "src/agentalloy/lm_client.py"
    - "src/agentalloy/config.py"
    - "src/agentalloy/web/wizard_api.py"
created_at: 2026-07-13T00:00:00Z
---

# 02-gate-wiring-and-escape-hatch

## Task

Wire Gate 1.5 into `install_local_pack` (`install_pack.py`) — the single
chokepoint both the local and orchestrator (`install_pack`) paths funnel through,
so remote packs are gated too (DK8) — using Task 01's validator.

- **Placement (DK3):** call `validate_review_verdicts(...)` **after** Gate 1's
  success (~line 700) and **before** Gate 2 (the version gate, ~line 702). Only
  Gate-1 passers reach it. On failure return `{action: "review_failed", ...}` in
  the **same error shape** as Gate 1's `schema_invalid` block (`errors: [{skill_id,
  file, errors}]`, a `remediation` string via `PackValidationResult.format_errors`)
  — no new exit code (AC 10).
- **Escape hatch (DK7):** add `allow_unreviewed: bool = False` to
  `install_local_pack` (+ `install_pack`), a `--allow-unreviewed` CLI flag
  (mirror `--allow-lint-warnings` / `--allow-duplicates` in the argparser and
  dispatch), and thread it. When set, skip the gate and record
  `gate_1_5 = {"status": "bypassed", "reason": "--allow-unreviewed"}` in the
  result contract — loud, never silent (AC 6).
- **CLI backstop posture (DK6):** read
  `AGENTALLOY_INSTALL_REQUIRE_INDEPENDENT_REVIEW` (default off) and pass it as
  `require_independent` to the validator. Default = process-forcing + auditable;
  `=1` rejects `mode: self`. (AC 8 CLI half.)
- **Result contract:** the success path always carries a `gate_1_5` block
  (`{"status": "passed"|"bypassed", "mode": <reviewer.mode|null>}`), so telemetry
  and the web lane can read what happened.
- **No LLM/network** added to this path (AC 5) — it only calls Task 01's pure
  validator.

Closes **AC 4, AC 6**, the **CLI half of AC 8**; contributes **AC 1, AC 5,
AC 10**. Tests: end-to-end `install_local_pack` on a tmp fixture pack — blocked
without/with-stale/with-rejecting verdict, passes with a valid one, bypass
recorded, independence lever both ways, remote-path parity (stubbed fetch).
