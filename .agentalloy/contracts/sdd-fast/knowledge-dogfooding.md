---
phase: sdd-fast
task_slug: knowledge-dogfooding
route: fast
domain_tags:
  - knowledge-module
scope:
  touches:
    - "docs/solutions/test-knowledge-trace.md"
  avoids:
    - "src/agentalloy/code_index/engine/**"
success_criteria: []
related_contracts:
  - ".agentalloy/contracts/intake/knowledge-dogfooding.md"
created_at: 2026-07-16T00:31:47Z
---

# Knowledge Dogfooding

## The change

No code change — this is verification-only. Confirms the already-shipped
Knowledge module (`agentalloy knowledge why <symbol>`, decision → governed-
symbol linkage) actually works end-to-end on this repo, since the
`knowledge-management-production` work-item's premise depends on it.

## Done when

Verified, 2026-07-16. Seeded `docs/solutions/test-knowledge-trace.md` with a
decision backtick-mentioning `agentalloy.src.agentalloy.config.Settings`
(already ingested by this repo's live code index):

- **Positive**: `agentalloy knowledge why
  agentalloy.src.agentalloy.config.Settings` returns exactly the one decision
  that mentions it, with correct `file_path`/`start_line`/`heading`/`snippet`
  (checked via `--json`).
- **Negative control**: the same command against an unrelated symbol
  (`agentalloy.src.agentalloy.api.proxy_signal.evaluate_signal`) returns
  `(no governing decisions)` — confirms the linkage is real (backtick-fenced
  symbol match), not generic search noise returned for any query.

Both acceptance criteria from the original request are met:
`agentalloy knowledge why <symbol>` returns a non-empty response containing
the decision text, and correctly references the source file.
