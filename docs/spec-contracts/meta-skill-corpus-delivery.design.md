---
phase: design
task_slug: meta-skill-corpus-delivery
route: full
related_spec: docs/spec-contracts/meta-skill-corpus-delivery.spec.md
domain_tags:
  - skill-pack-authoring
  - yaml-schema
  - system-skill
  - workflow-gates
scope:
  touches:
    - "src/agentalloy/_packs/sdd/pack.yaml"                       # register 2 new skill files
    - "src/agentalloy/_packs/sdd/sys-skill-authoring-rules.yaml"  # NEW — converted from meta/
    - "src/agentalloy/_packs/sdd/sys-skill-review-verdict.yaml"   # NEW — converted from meta/
    - "src/agentalloy/_packs/sdd/sdd-add-skill.yaml"              # requires: edges (see DK5)
    - "tests/**"
  avoids:
    - "src/agentalloy/retrieval/**"     # Option A needs zero retrieval-code changes
    - "src/agentalloy/ingest.py"        # existing system-class path is reused as-is
    - "src/agentalloy/install/importer.py"
    - "RETRIEVAL_GRAPH_EXPAND"          # not touched — Option A doesn't need it
success_criteria: []
related_contracts:
  - docs/spec-contracts/meta-skill-corpus-delivery.spec.md
created_at: 2026-07-14T00:00:00Z
---

# Meta-Skill Corpus Delivery — Design (Option A)

## Scope in a sentence

Make `sys-skill-authoring-rules` and `sys-skill-review-verdict` — the two meta
skills `sdd-add-skill` actually references — real, phase-scoped, retrievable
system skills, by converting them from loose bootstrap-markdown into YAML skill
files inside the `sdd` pack, riding the exact mechanism the working `sys-ci` skill
already proves. Closes spec AC 1, 3, 5 for these two skills specifically.

## Design

Approach, task plan, and test cases live in the `meta-skill-corpus-delivery.design/`
folder (`approach.md`, `tasks.md`, `test-plan.md`), resolving decisions as
**DK1–DK6**. Acceptance is fixed by `meta-skill-corpus-delivery.spec.md` and is not
reopened here. Per-task build contracts (design→build hand-off) are in
`meta-skill-corpus-delivery.build/`.

**Scope is deliberately narrow — see `approach.md`'s Scope boundary.** Of the 9
meta/conventions skills the spec named, only these 2 have a confirmed live
consumer (`sdd-add-skill`). The other 7 are not converted, deleted, or decided
here — they may belong to a different package entirely, and guessing their fate
would be scope creep beyond what this pass can justify.
