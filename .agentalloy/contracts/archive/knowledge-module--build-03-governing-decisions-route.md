---
phase: build
task_slug: 03-governing-decisions-route
route: full
domain_tags:
  - code-index-query
  - fastapi-router
scope:
  touches:
    - "src/agentalloy/code_index/api/search_router.py"
    - "src/agentalloy/code_index/api/models.py"
    - "tests/**"
  avoids:
    - "src/agentalloy/code_index/engine/**"
    - "src/agentalloy/_corpus/**"
    - "src/agentalloy/signals/**"
    - "src/agentalloy/api/proxy_injection.py"
created_at: 2026-07-09T00:00:00Z
---

# 03-governing-decisions-route

## Task

Expose the `governing_decisions` structural query (task 01's store method) on the
code-index search router, returning a decision-shaped view.

- **`DecisionView`** (`api/models.py`): `qualified_name`, `file_path`,
  `start_line`, `heading`, `snippet` — **not** `CallSiteView` (whose `line` is a
  call-site and which drops the heading/body). Closer template: `SearchResult`.
- **Router** (`search_router.py`): add `"governing_decisions"` to
  `_STRUCTURAL_QUERIES` and `_FQN_QUERIES`; add the branch in
  `search_structural._run` calling `h.graph.governing_decisions(fqn)` and mapping
  to `DecisionView`. Reuse the existing 400-on-missing-`fqn` validation and
  `with_handles`.

No new index (`idx_edges_dst` covers `WHERE kind='GOVERNS' AND dst=?`). No new
route path — this rides `/code/search/structural`.

## Test cases

- TC5 (AC 5): `GET /code/search/structural?query=governing_decisions&fqn=…`
  returns a `DecisionView` list carrying heading + snippet; missing `fqn` → 400;
  ungoverned fqn → `[]`.
</content>
