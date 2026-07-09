# Knowledge module, slice 1 — type, link & query

The decision layer over the code index: a `GOVERNS` edge from a decision (an
existing `MarkdownDoc` heading-chunk) to the code symbols it governs, plus the
structural query and `agentalloy knowledge why <symbol>` pull verb. Built
test-first, dogfooding the SDD lifecycle end-to-end (spec → design → build → qa).

Tags: knowledge-module, code-index, symbol-linkage, sdd-dogfooding

## Problem

AgentAlloy's third context module (Knowledge — "why is the code this way?") needed
its first indexed slice. The open questions weren't "what to build" (the spec
settled that) but "how, against the real code": how to represent a decision
without a schema migration, how to link it to code deterministically, and how to
keep links correct across incremental re-index — without an LLM in the path.

## Approach that worked

- **Ground before deciding.** Three read-only exploration passes over the real
  store/ingest/query code turned every design "how" into a decision backed by a
  `file:line`. The load-bearing find: markdown chunks *already* ride the DuckDB
  `symbols` table (`_markdown_symbol`, `kind="MarkdownDoc"`), so a decision **is**
  a graph row — `GOVERNS` is just an ordinary `edges` row and the "what governs
  this?" query is a one-line clone of `callers()`. No parallel store, no DDL.
- **Overlay, don't mutate.** Decisions stay `MarkdownDoc` rows; the typed layer is
  a `GOVERNS` edge kind plus a decision-bearing predicate. This sidesteps a
  phase-ordering race: `_index_markdown` re-upserts every changed chunk with the
  literal `MarkdownDoc` kind, so a reclassified kind would be clobbered.
- **Match granularity to the delete primitive.** `delete_for_files` deletes edges
  by `file_path` regardless of kind — doc-granular. So the re-derive is
  doc-granular too: every source doc with ≥1 changed/removed chunk has all its
  `GOVERNS` edges dropped and re-derived over its *current* chunks.
- **TDD per build contract**, one dominant surface each (store → ingest → route →
  CLI → guards), red before green.

## Verification

37 knowledge tests (store round-trip, linkage precision, doc-granular lifecycle,
route, CLI, boundary guards) plus the full non-integration suite: 3981 passed,
ruff/format/pyright clean. The deterministic SDD gates judged each artifact — the
spec and design exit gates passed and correctly halted at human approval.

## What didn't work

- **A grounding agent asserted markdown is *not* in the graph store.** It was
  wrong — it conflated the retrieval *hydrate* fallback with storage. Two agents
  contradicting each other on a load-bearing fact is a stop-the-line signal:
  verify it directly, don't average the opinions.
- **The first edge-lifecycle design violated AC 3.** A chunk-granular re-derive
  keyed on `changed`/`removed` would let `delete_for_files` wipe an *unchanged*
  sibling decision's links and never restore them. An adversarial coherence
  reviewer caught it before a line was written; the fix was doc-granular re-derive.
- **Tier-2 name resolution nearly shipped a false positive.** Linking a fenced
  span that matches exactly one symbol name links `` `run` ``/`` `build` `` — a
  coincidental English-word match. Needed a "code-shaped" guard (namespace/path
  separator, internal underscore, or internal CamelCase).
- **The CLI test hit the real service.** `knowledge.py` imported `_make_client`
  by name, so monkeypatching `code._make_client` didn't reach it. The seam must
  resolve through the `code` module at call time.
- **The `design→build` density gate is repo-global** (filed as #378). It sums
  tasks across all `docs/design/**/tasks.md` and build contracts across all of
  `.agentalloy/contracts/build/`, so a fully-decomposed item can't satisfy it when
  sibling items are undecomposed. Advanced with `--force` after confirming *this*
  item's decomposition was complete.

## Decision worth keeping

- **`GOVERNS` is a free-form edge kind — zero DDL.** The `symbols`/`edges` tables
  use free-form `TEXT` kinds with no enum/FK; a typed layer adds no migration. The
  graph is "rebuilt not migrated," so encode new types in the kind, not the schema.
- **Adversarial verification pays for itself on design, not just code.** The two
  most expensive errors (the AC 3 lifecycle bug, the tier-2 false positive) were
  caught by reviewers reading the *design doc* against the real code — before any
  implementation. Ground first, then have skeptics attack the grounded design.
- **`knowledge why` keeps a distinct CLI namespace over the shared `/code`
  route.** Decisions are a typed overlay on the same store (shared query rail), but
  the module boundary shows up where users meet it — on the command line.
