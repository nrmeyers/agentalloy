---
phase: build
task_slug: 02-index-decisions-phase
route: full
domain_tags:
  - code-index-ingest
  - symbol-linkage
scope:
  touches:
    - "src/agentalloy/code_index/ingest/pipeline.py"
    - "tests/**"
  avoids:
    - "src/agentalloy/code_index/engine/**"
    - "src/agentalloy/_corpus/**"
    - "src/agentalloy/signals/**"
    - "src/agentalloy/api/proxy_injection.py"
created_at: 2026-07-09T00:00:00Z
---

# 02-index-decisions-phase

## Task

Add the deterministic `_index_decisions` phase to `pipeline.py`, run **after**
`_index_markdown` in `run_index_job`. It overlays `GOVERNS` edges (via task 01's
store methods) from decision chunks to the code symbols they govern.

- **Return the sets:** make `_index_markdown` return its `changed`/`removed`
  chunk sets (it yields only a count today) so this phase can act on them.
- **Affected docs (doc-granular, per DK6):** `affected = {doc path : ≥1 chunk in
  changed ∪ removed AND path matches the source allow-list}`. Allow-list =
  `docs/solutions/*.md`, `docs/design/*/approach.md`,
  `docs/spec-contracts/*.design/approach.md` (configurable constant;
  `docs/ship`/`docs/qa` excluded by default). For each affected doc:
  `delete_govern_edges_for_doc(doc)` then re-derive over **every current decision
  chunk of that doc** (an unchanged sibling's edges are restored in the same pass
  — the AC 3 correctness fix).
- **Linkage (DK2):** for each candidate chunk, extract inline-code spans from the
  chunk body; resolve each span by (1) exact non-`MarkdownDoc` fqn
  (`graph.symbol`), then (2) unambiguous **code-shaped** short name
  (`symbols_by_name`, single hit, span carries `.`/`::`/`/`/`_`/internal-caps —
  drop bare dictionary words). Drop ambiguous / non-code-shaped / unresolved. A
  candidate *becomes* a decision iff ≥1 governed symbol; `dst` is always a code
  fqn.

Deterministic, DB-only, no LM/cloud. Inherit the content-hash incremental skip
from the markdown rows (do not re-walk).

## Test cases

- TC3 (AC 3): content-unchanged re-index preserves edges; edit-drops-ref prunes;
  chunk-removal prunes; **doc with removed chunk A + unchanged decision chunk B →
  B's links survive** (doc-granular re-derive).
- TC4 (AC 4): one known code-shaped symbol → links that fqn only; ambiguous bare
  name / non-symbol path / **fenced common word matching one symbol** / a
  `path::anchor` span → link nothing.
- TC2 (AC 2): sources are only the allow-listed lifecycle paths; nothing read/
  written under `docs/architecture-decisions/` or `CLAUDE.md`.
</content>
