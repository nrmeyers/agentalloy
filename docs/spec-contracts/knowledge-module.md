---
phase: spec
task_slug: knowledge-module
route: full
domain_tags:
  - knowledge-module
  - decision-records
  - code-index-graph
  - symbol-linkage
  - just-in-time-injection
scope:
  touches:
    - "src/agentalloy/code_index/store/graph_store.py"
    - "src/agentalloy/storage/protocols.py"
    - "src/agentalloy/code_index/ingest/pipeline.py"
    - "src/agentalloy/code_index/retrieval/**"
    - "src/agentalloy/code_index/api/search_router.py"
    - "src/agentalloy/install/subcommands/**"
    - "src/agentalloy/_packs/sdd/**"
    - "docs/spec/knowledge-module.md"
    - "tests/**"
  avoids:
    - "src/agentalloy/code_index/engine/**"
    - "src/agentalloy/_corpus/**"
    - "src/agentalloy/api/proxy_injection.py"
success_criteria: []
related_contracts: []
created_at: 2026-07-09T00:00:00Z
---

# knowledge-module

## Scope in a sentence

Build AgentAlloy's third context module — **Knowledge**: the decisions behind
the code and *why* they were made — as a typed layer over the existing code
index (a `Decision` node + `GOVERNS` edge linking a decision to the symbols it
governs) that *composes* the "why" the SDD lifecycle already writes
(`docs/solutions/`, the design phase's `approach.md`, the ADR/rationale corpus)
rather than adding a new capture ritual beyond the shipped codify gate, and
surfaces it just-in-time at design/build.

## Spec

Acceptance criteria and out-of-scope live in `docs/spec/knowledge-module.md`.

> Runtime/tracked note: the committed spec doc is
> `docs/spec-contracts/knowledge-module.spec.md`; to arm a live SDD run, copy it
> to `docs/spec/knowledge-module.md` (dropping the `.spec` infix, so the spec
> exit-gate's `docs/spec/*.md` glob finds it) and this contract to
> `.agentalloy/contracts/spec/knowledge-module.md` (git-ignored runtime path).
> This is a valid `contracts.py` artifact. It is deliberately module-scoped: the
> spec doc lays out a **sliced** delivery (slice 0 — codify+promote — already
> shipped in #375), so design will fan it into per-slice work rather than one
> build.
