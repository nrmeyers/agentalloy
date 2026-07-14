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

## Scope boundary (deliberately narrow — read before extending)

The spec named **9** meta/conventions skills total. Of those, only **2** have a
confirmed live consumer: `sdd-add-skill.yaml` (a real, retrieved, `applies_to_phases:
[add-skill]` workflow skill) references `sys-skill-authoring-rules` and
`sys-skill-review-verdict`. The other 7 (`sys-r1-tiered-sourcing`,
`sys-skill-transform-contract`, `sys-skill-tagging-rules`,
`sys-fragment-types-and-sizing`, `sys-skill-naming`, `sys-skill-output-formatting`,
`sys-skill-writing-voice`) are referenced **only by other meta skills**, never by
anything in the retrievable corpus — they read like the skill-*authoring* toolchain
(fragment sizing, tagging, transform-contract, voice/naming conventions), which may
belong to the separate `agentalloy-authoring` package rather than this repo's
runtime corpus at all.

**This design does not decide their fate.** Converting them on the same guess
would be scope creep into a question (which package owns skill-authoring tooling)
that isn't this design's to answer. If a later consumer references one of the 7
from a real retrieved skill, it gets the same treatment via the same pattern this
design establishes — cheap to extend, not free to guess now.

## Decisions

### DK1 — Delivery target for this pass → **`sys-skill-authoring-rules` + `sys-skill-review-verdict` only** (see Scope boundary)

### DK2 — Pack placement → **fold into the existing `sdd` pack, not a new `meta` pack**

The sole consumer (`sdd-add-skill`) already lives in `sdd/`. A new `_packs/meta/pack.yaml`
would need its own `install-packs` registration, tier metadata
(`PACK_METADATA`/`PACK_TIERS` in `scripts/migrate-seeds-to-packs.py`, per
`tests/install/test_pack_tier_registry_consistency.py`), and dependency wiring for
zero benefit — nothing outside `sdd` needs these two skills. Folding in also means
one version bump, one pack directory, matches how `sys-ci` (a system skill *about*
the build/qa phases) already lives alongside its own domain rather than in a
separate `sys`-everything pack. Rejected: a new `meta` pack (more machinery, no
consumer outside sdd); leaving them in `_packs/meta/*.md` and writing a bespoke
loader (Option C, rejected by the user's "A" choice already).

### DK3 — Per-skill applicability metadata → **`phase_scope: [add-skill]`, `category_scope: null`, `always_apply: false`, both skills**

Mirrors `sys-ci`'s proven shape exactly (`phase_scope: [build, qa]`,
`category_scope: null`, `always_apply: false`) — the working reference this design
is built on. Satisfies `_is_applicable` rule 2 (phase matches, no category
restriction) without ever going through `category_scope` (which `compose.py`
always calls with `None` regardless). Rejected: `always_apply: true` — would
inject into *every* session in the repo, not just add-skill ones, for content that
is irrelevant outside that phase.

### DK4 — Conversion shape → **YAML `skill_class: system`, `raw_prose` only, no `fragments:` list**

Verified against `install/importer.py::import_skill`: for `skill_class == "system"`,
the importer auto-generates one `guardrail` fragment from `raw_prose` — the YAML
never needs a `fragments:` list (unlike `domain`, which iterates `data.get("fragments",
[])`). This makes the conversion mechanical: take each `.md`'s header fields
(`skill_id`, `category`, `author`, `change_summary`) verbatim, take everything
after the header block as `raw_prose` verbatim (no re-authoring, no R2-R4
fragment-lint concerns — those rules govern `domain` skills), set `skill_class:
system`, and the three DK3 fields. `description` is optional
(`ingest.py` defaults it to `""`) — omit unless a one-line summary earns its
keep. Verified no other field is required for `skill_class: system` by
`ingest._validate`.

### DK5 — Make `sdd-add-skill`'s references real → **add `requires:` edges, keep the prose reference too**

`sdd-add-skill.yaml` currently *names* both skills in prose only. Add
`requires: [sys-skill-authoring-rules, sys-skill-review-verdict]` to its YAML
header. This is not load-bearing for delivery under Option A (phase-scoped system
retrieval doesn't consult `requires` — that's the domain/graph-expand path, DK-
irrelevant here since `RETRIEVAL_GRAPH_EXPAND` stays untouched per spec AC 4) —
it's **provenance**: a human or a future audit reading `sdd-add-skill.yaml` sees a
structured dependency, not just prose that happens to name an id. Keep the prose
too; an agent composing the workflow still needs the instruction in plain language,
`requires:` is metadata, not a substitute for the sentence.

### DK6 — `sys-skill-review-verdict` re-read for full-injection delivery → **no changes needed, verified against the flagged spec concern**

The spec's design-surface item asked whether the producer skill (authored assuming
reference-doc-style access) needs rework now that it injects in full via system
retrieval (unranked, untruncated — unlike domain fragments). Reviewed: the skill
is ~900 words of directive prose with no reference-doc framing assumptions (no
"see the corpus for X" hedging beyond the one `sys-skill-transform-contract`
disambiguation, which is correct to keep). It reads correctly as a full injected
guardrail. No edit needed.

### DK7 — Phase-value linchpin, verified end-to-end (not assumed)

The whole mechanism depends on the runtime `phase` value at the
`retrieve_system_fragments` call site actually equaling the string `"add-skill"` —
not just that `sdd-add-skill.yaml` declares `applies_to_phases: [add-skill]` (a
different field, on the workflow-skill side, that does not by itself prove the
retrieval-side value matches). Traced the full chain before committing to build:

1. `"add-skill"` is a canonical, first-class phase —
   `ingest._VALID_PHASES = {..., "add-skill"}` and `gates._PHASE_GRAPH["add-skill"]
   = "intake"` (the post-approval phase-return target). Not invented for this
   design.
2. The literal command `agentalloy phase set add-skill` already exists in shipped
   `sdd-intake.yaml` prose (`"phase set add-skill"` is one of intake's routing
   options) — this is how a session actually enters the phase.
3. `install/subcommands/phase.py::run_phase_set` validates against the same
   `VALID_PHASES` set — `add-skill` is accepted, not rejected.
4. `api/proxy_context.py::read_phase` → `signals/skill_loader._read_phase` reads
   `.agentalloy/phase` and returns the **stripped string verbatim** — no
   translation table, no renaming.
5. `orchestration/compose.py` passes `phase=req.phase` straight into
   `retrieve_system_fragments(phase=req.phase, category=None)` — `req.phase` is
   the same verbatim string from step 4.

Chain confirmed unbroken, end to end, by reading the actual code at each hop —
not inferred from `applies_to_phases` alone (the trap: that field governs
workflow-skill *retrieval* eligibility via the embedding/domain path, a different
mechanism from the system-skill predicate this design relies on). `sys-ci`'s
`phase_scope: [build, qa]` remains the closest working precedent, but `build`/`qa`
being canonical didn't by itself prove `add-skill` behaves the same way — DK7 is
that proof, not an inference from analogy.

## What stays untouched (boundary guards)

- **No retrieval code changes.** `retrieval/system.py`, `applicability.py`,
  `retrieval/domain.py` are unmodified — Option A's entire value is reusing the
  existing predicate path as-is.
- **`RETRIEVAL_GRAPH_EXPAND` stays off, untouched.** Per spec AC 4 — Option A does
  not need it; flipping it is a separate, global decision this design does not make.
- **The 7 out-of-scope meta skills are not converted, not deleted, not decided.**
  They remain exactly as-is in `_packs/meta/`/`_packs/conventions/` pending a
  future consumer.
- **`install-pack-semantic-gate`'s dormant flag is untouched.** Unrelated feature;
  this design does not flip `AGENTALLOY_INSTALL_REQUIRE_REVIEW`.
