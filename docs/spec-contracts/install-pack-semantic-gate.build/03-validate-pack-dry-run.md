---
phase: build
task_slug: 03-validate-pack-dry-run
route: full
domain_tags:
  - install-pack
  - validate-pack
scope:
  touches:
    - "src/agentalloy/install/subcommands/validate_pack.py"
    - "tests/**"
  avoids:
    - "src/agentalloy/api/**"
    - "src/agentalloy/orchestration/**"
    - "src/agentalloy/lm_client.py"
    - "src/agentalloy/config.py"
created_at: 2026-07-13T00:00:00Z
---

# 03-validate-pack-dry-run

## Task

Extend `validate_pack` (`validate_pack.py`) — the zero-side-effect dry-run of
install-pack's gates — to also report Gate 1.5 status, using Task 01's validator.

- After the existing Gate-1 schema/lint block, call
  `validate_review_verdicts(pack_dir, skills_entries)` and fold its findings into
  the same contract-shaped result dict (add a `review` section or merge into the
  aggregated errors, matching the existing `valid`/`invalid` action semantics —
  a failing verdict makes the pack `invalid`, exit 1).
- **Preserve the module's guarantee:** no ingest, no reembed, no network, no
  corpus mutation of any kind (AC 9). The validator is pure; assert it in a test
  (corpus row count unchanged; no store written).
- Report is informational for a passing verdict (present / fresh / approving) and
  blocking for a failing one, mirroring how Gate 1 lint errors flip `action` to
  `invalid`.
- Do not import `lm_client` or touch the network (AC 5).

Closes **AC 9**; contributes **AC 5**. Tests: `validate-pack` on a pack with a
failing verdict → `invalid` + exit 1 + zero side effects; with a passing verdict
→ `valid`; a grep guard that the path never imports `lm_client`/`authoring`.
