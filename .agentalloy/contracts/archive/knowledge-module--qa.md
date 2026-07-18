---
phase: qa
task_slug: knowledge-module
route: full
domain_tags:
  - knowledge-module
  - code-index-graph
scope:
  touches:
    - "tests/**"
    - "docs/solutions/**"
  avoids:
    - "src/agentalloy/code_index/engine/**"
    - "src/agentalloy/_corpus/**"
created_at: 2026-07-09T00:00:00Z
---

# knowledge-module

## Scope in a sentence

Verify Knowledge slice 1 (type, link & query) — the store/ingest/route/CLI
tests are green and the boundary guards hold — and codify the build's lesson to
`docs/solutions/knowledge-module.md` before ship.
