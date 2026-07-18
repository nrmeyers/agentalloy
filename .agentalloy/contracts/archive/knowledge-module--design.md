---
phase: design
task_slug: knowledge-module
route: full
domain_tags:
  - knowledge-module
  - decision-records
  - code-index-graph
  - symbol-linkage
  - code-index-query
scope:
  touches:
    - "src/agentalloy/code_index/store/graph_store.py"
    - "src/agentalloy/storage/protocols.py"
    - "src/agentalloy/code_index/ingest/pipeline.py"
    - "src/agentalloy/code_index/api/search_router.py"
    - "src/agentalloy/code_index/api/models.py"
    - "src/agentalloy/install/subcommands/code.py"
    - "docs/**"
    - "tests/**"
  avoids:
    - "src/agentalloy/code_index/engine/**"
    - "src/agentalloy/_corpus/**"
    - "src/agentalloy/api/proxy_injection.py"
    - "src/agentalloy/signals/**"
success_criteria: []
related_contracts: []
created_at: 2026-07-09T00:00:00Z
---

# knowledge-module

## Scope in a sentence

Design **slice 1** of the Knowledge module — *type, link & query* — a new
deterministic `_index_decisions` ingest phase that overlays a typed `GOVERNS`
edge from a decision (an existing `MarkdownDoc` heading-chunk) to the code
symbols it governs, plus the structural query and CLI pull verb that read it
back, resolving the spec's six open "how" decisions (DK1–DK6) against real code —
without touching injection (slice 2, AC 6), the read-path engine, or the corpus.

## Design

Approach, task plan, and test cases live in the
`knowledge-module.design/` folder (`approach.md`, `tasks.md`, `test-plan.md`).
Acceptance is fixed by `docs/spec-contracts/knowledge-module.spec.md`
(`## Acceptance Criteria`, AC 1–9) and is **not** reopened here; tasks name the
`AC-N` they satisfy. This design covers **slice 1 only** — AC 1–5, 7–9. AC 6
(just-in-time injection) is **slice 2** and is explicitly out of scope for this
design; the injection code paths (`api/proxy_injection.py`, `signals/`) are in
this contract's `avoids`.

> Location note. At runtime this contract's home is
> `.agentalloy/contracts/design/knowledge-module.md` and the design docs' is
> `docs/design/knowledge-module/` — both git-ignored SDD runtime paths. The
> committed, reviewable copies live under `docs/spec-contracts/`: this contract,
> and the `knowledge-module.design/` folder for the three design docs. The
> per-task build contracts (design→build hand-off) will land in
> `knowledge-module.build/` as slice 1 is built.
</content>
</invoke>
