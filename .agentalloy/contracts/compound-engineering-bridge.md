---
phase: spec
task_slug: compound-engineering-bridge
route: full
domain_tags:
  - compound-engineering
  - workflow-lifecycle
  - exit-gates
  - skill-authoring
  - knowledge-capture
scope:
  touches:
    - "src/agentalloy/_packs/sdd/sdd-verify-and-review.yaml"
    - "src/agentalloy/signals/predicates.py"
    - "src/agentalloy/api/proxy_signal.py"
    - "src/agentalloy/contracts.py"
    - "src/agentalloy/install/subcommands/**"
    - "docs/solutions/**"
    - "docs/spec/compound-engineering-bridge.md"
    - "tests/**"
  avoids:
    - "src/agentalloy/code_index/**"
    - "src/agentalloy/retrieval/**"
    - "src/agentalloy/api/**"
    - "src/agentalloy/_corpus/**"
# Per the spec-phase template, acceptance has a single home: the spec doc's
# `## Acceptance Criteria`. Left empty here to avoid a second, driftable copy.
success_criteria: []
related_contracts: []
created_at: 2026-07-08T00:00:00Z
---

# compound-engineering-bridge

## Scope in a sentence

Give AgentAlloy's own SDD lifecycle a *compounding* step — a lessons artifact
written at ship, a deterministic gate that enforces it, and a promotion path
from that artifact into the instruction corpus — so compound engineering's
knowledge **write-path** and AgentAlloy's context **read-path** close into one
loop, reusing the code index, signal layer, and pack rail that already ship.

## Spec

Acceptance criteria and out-of-scope live in `docs/spec/compound-engineering-bridge.md`
(the runtime path). The committed copy of that spec doc is
`docs/spec-contracts/compound-engineering-bridge.spec.md`.

> Runtime note: a live contract's home is
> `.agentalloy/contracts/spec/compound-engineering-bridge.md`, and the spec doc's
> is `docs/spec/compound-engineering-bridge.md` — both git-ignored SDD runtime
> paths (`.gitignore` lines 68, 99). The tracked copies under `docs/spec-contracts/`
> are the committed, reviewable form of that spec-phase work-item; this contract is
> a valid `contracts.py` artifact and can be dropped into the runtime path verbatim.
> To arm a live run, copy this file to
> `.agentalloy/contracts/spec/compound-engineering-bridge.md` and the spec doc to
> `docs/spec/compound-engineering-bridge.md` (dropping the `.spec` infix so the
> spec exit-gate's `docs/spec/*.md` glob finds it).
