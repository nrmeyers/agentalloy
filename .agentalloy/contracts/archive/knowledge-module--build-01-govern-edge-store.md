---
phase: build
task_slug: 01-govern-edge-store
route: full
domain_tags:
  - code-index-graph
scope:
  touches:
    - "src/agentalloy/code_index/store/graph_store.py"
    - "src/agentalloy/storage/protocols.py"
    - "tests/**"
  avoids:
    - "src/agentalloy/code_index/engine/**"
    - "src/agentalloy/_corpus/**"
    - "src/agentalloy/signals/**"
    - "src/agentalloy/api/proxy_injection.py"
created_at: 2026-07-09T00:00:00Z
---

# 01-govern-edge-store

## Task

Add the store primitives for the `GOVERNS` overlay to `CodeGraphStore`
(`graph_store.py`) and mirror them on the `CodeGraph` protocol
(`storage/protocols.py`). `GOVERNS` is a free-form `edges.kind` — **no DDL**,
reuse the existing `symbols`/`edges` tables and the `idx_edges_dst` index.

- `symbols_by_name(name) -> list[(qualified_name, kind)]` — `SELECT
  qualified_name, kind FROM symbols WHERE name = ? AND kind != 'MarkdownDoc'`.
  The only symbol getter today is exact-PK `symbol()`; this is the new by-name
  lookup DK2 tier-2 needs. Unindexed `name` scan is acceptable at slice-1 scale.
- `governing_decisions(fqn) -> list[DecisionRow]` — clone `callers()` (the
  `WHERE e.kind='CALLS' AND e.dst=?` + `LEFT JOIN symbols` shape) with
  `e.kind='GOVERNS'`, reading `e.src` (the decision chunk) and hydrating its
  `file_path`, `start_line`, `name` (heading), `source_code` (snippet). One hop,
  not transitive.
- `delete_govern_edges_for_doc(doc_path)` — `DELETE FROM edges WHERE
  kind='GOVERNS' AND file_path = ?` — the doc-granular prune DK6 requires.
- `upsert_govern_edges([...])` — write `GOVERNS` edges via the existing
  `upsert_edges`, each with `src=decision qn`, `dst=code fqn`, `file_path=doc`.

Keep everything deterministic and side-effect-free beyond the writes. Model the
read on `callers()`; reuse `graph.conn`.

## Test cases

- TC1 (AC 1): write a `GOVERNS` edge (decision qn → code fqn) and read it back via
  `governing_decisions(fqn)` — no schema/ALTER change. Decision fields (heading,
  file_path, start_line) hydrate.
- TC5a (AC 5): `governing_decisions` returns `[]` for an ungoverned fqn.
- `symbols_by_name`: exact-name match excludes `MarkdownDoc` rows; multi-match
  returns all (caller resolves ambiguity).
- `delete_govern_edges_for_doc`: removes only the named doc's `GOVERNS` edges,
  leaves other docs' and non-`GOVERNS` edges intact.
</content>
