---
phase: intake
task_slug: knowledge-dogfooding
route: fast
domain_tags:
  - knowledge-module
scope:
  touches:
    - "src/agentalloy/api/knowledge_push.py"
    - "src/agentalloy/code_index/ingest/markdown.py"
    - "src/agentalloy/code_index/store/graph_store.py"
  avoids:
    - "src/agentalloy/code_index/engine/**"
success_criteria: []
related_contracts: []
created_at: 2026-07-15T22:36:29Z
---

# knowledge-dogfooding

## What the user actually wants

Verify the already-shipped Knowledge module (decision → governed-symbol
linkage, `agentalloy knowledge why <symbol>`) end-to-end on this repo, by
enabling it locally (`CODE_INDEX_ENABLED=1`), triggering an index, and
confirming a real decision query returns the correct, source-linked answer.
This is validation of existing code, not new development — the
`knowledge-management-production` work-item's "already validated locally"
claim rests on this passing.

## Intent signals

- intent: investigate
- artifact_type: spike (verification only; only routes to a fix if a bug is
  found)
- scope: small — local-only env override, no code change unless a bug
  surfaces
- urgency: now — gates the productionization work-item's premise

## Proposed route

fast — this is a bounded local verification pass with a small, checkable
acceptance list (see the success criteria below), not a multi-phase feature.
If it turns up a genuine bug, that becomes its own full-route spec, per the
original scoping note.

## Success criteria (carried from the original request)

- `agentalloy knowledge why <symbol>` returns a non-empty response containing
  the decision text, correctly referencing the source file.
- The query succeeds once indexing has completed (`agentalloy code index`
  reaches 100%).

## Out of scope

- The Knowledge Graph *configuration* CLI (setup wizard / config subcommands /
  upgrade reminder) — that's `knowledge-management-production`, a separate
  work-item.
- Decision extraction from non-markdown files.
