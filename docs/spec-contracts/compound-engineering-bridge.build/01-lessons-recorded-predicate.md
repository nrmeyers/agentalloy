---
phase: build
task_slug: 01-lessons-recorded-predicate
route: full
domain_tags:
  - deterministic-predicates
scope:
  touches:
    - "src/agentalloy/signals/predicates.py"
    - "src/agentalloy/api/proxy_signal.py"
    - "src/agentalloy/contracts.py"
    - "tests/**"
  avoids:
    - "src/agentalloy/_packs/**"
    - "src/agentalloy/code_index/**"
success_criteria: []
related_contracts: []
created_at: 2026-07-08T00:00:00Z
---

# 01-lessons-recorded-predicate

## Task

First factor `_resolve_current_contract` down from `api/proxy_signal.py:164` into
`contracts.py` (or `skill_loader.py`) and update the proxy to call it there, so
`signals` can reuse it without importing `api`. Then add a deterministic, DB-free
predicate `lessons_recorded` to `signals/predicates.py`, registered in
`PREDICATES`. It resolves the **active task slug** via that shared resolver
against `ctx.current_phase` (cursor-first → sole-contract → `(None, None)` for an
uncursored fan-out; strict, no mtime guess — the cursor is seeded on phase entry so
it is reliably set, per D6/Outcome B), takes
`Path(...).stem` as the slug, and returns `MET` iff `docs/solutions/<slug>.md`
exists, `NOT_MET` if not, and `UNKNOWN` when nothing resolves. Model it on
`eval_approval_recorded`; reuse `_glob_files`; keep it side-effect-free. Honor the
cursor first — don't call `latest_contract` directly (it ignores the cursor).

## Test cases

- TC1 (AC 1): `NOT_MET` with no `docs/solutions/<slug>.md`, `MET` once present.
- TC2 (AC 2): only `docs/solutions/<other>.md` present → `NOT_MET` for `<slug>`.
- Edge: no single work-item resolvable → `UNKNOWN` (never raises).
- Refactor: proxy banner slug resolution unchanged after the resolver moves.
