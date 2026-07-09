---
phase: design
task_slug: knowledge-module-injection
route: full
domain_tags:
  - knowledge-module
  - decision-injection
  - jit-compose
  - proxy-injection
  - code-index-graph
scope:
  touches:
    - "src/agentalloy/api/proxy_apply.py"
    - "src/agentalloy/api/knowledge_push.py"
    - "src/agentalloy/code_index/store/graph_store.py"
    - "src/agentalloy/storage/protocols.py"
    - "src/agentalloy/contracts.py"
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

# knowledge-module-injection

## Scope in a sentence

Design **slice 2** of the Knowledge module — *just-in-time injection* (AC 6): at
the design/build phase, when a code symbol governed by a decision falls in the
task contract's `scope.touches`, front-load that decision's rationale into
context **without the agent asking** — a deterministic, structural push folded
into the existing composed block (last user message, never the prompt-cached
system field), deferring to Instructions when a promoted skill already covers the
decision and excluding superseded decisions.

## Design

Approach, task plan, and test cases live in the
`knowledge-module-injection.design/` folder (`approach.md`, `tasks.md`,
`test-plan.md`). Acceptance is fixed by
`docs/spec-contracts/knowledge-module.spec.md` (`## Acceptance Criteria`, **AC 6**;
boundary AC 9) and is **not** reopened here; tasks name the `AC-N` they satisfy.
This design covers **slice 2 only** — the push. Slice 1 (type, link & query,
AC 1–5, 7–9) shipped on branch `claude/compound-engineering-agentalloy-65zzy9`
(PR #379) and this slice **stacks on it** — it consumes slice 1's `GOVERNS`
edges and `governing_decisions`.

> Location note. Runtime homes are `.agentalloy/contracts/design/…` and
> `docs/design/knowledge-module-injection/…` (git-ignored). The committed,
> reviewable copies live here under `docs/spec-contracts/`.
>
> Dependency note. Slice 2 modifies the injection/compose runtime that slice 1
> deliberately **avoided**. It must not touch the prompt-cached system block
> (`proxy_injection.py`'s `system`/`instructions` field) and degrades to a no-op
> when the code index is disabled or unindexed.
</content>
