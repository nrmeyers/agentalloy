# Knowledge Module — Slice 1 (Type, Link & Query) — Design

> Runtime home: `docs/design/knowledge-module/approach.md` (git-ignored). This is
> the committed copy. Acceptance lives in
> `docs/spec-contracts/knowledge-module.spec.md` (`## Acceptance Criteria`, AC 1–9)
> and is not reopened here. This design covers **slice 1 (type, link & query)** —
> AC 1–5, 7–9. **Slice 2 (injection, AC 6) is out of scope** and its code paths
> are in the contract's `avoids`.

## Approach

Slice 1 turns the "why" the SDD lifecycle already writes into a **typed,
queryable, symbol-linked** layer — with no new authoring ritual and no schema
migration. It is one new **deterministic ingest phase** (`_index_decisions`) that
runs after markdown ingest and emits a new `GOVERNS` edge kind, plus the two
**read** front doors that consume it: a structural route and a CLI pull verb.
Nothing here touches injection (slice 2), `code_index/engine/`, or the corpus.

The whole design rests on one grounded fact that resolves the representation
question outright, so it is stated first.

### Grounding that reshaped the design — a decision is already a graph row

The spec left open (Design surface, "Node/edge representation") whether decisions
should reclassify the existing markdown rows or be a parallel typed layer. Reading
the code settles it: **markdown heading-chunks already ride the DuckDB `symbols`
table** — `_markdown_symbol` writes them with `kind="MarkdownDoc"`,
`qualified_name = "{relpath}::{anchor}"`, `name = heading`, `source_code = body`,
`start_line`/`end_line = heading offsets`
(`code_index/ingest/pipeline.py:115-132`, upserted at `:466`). Its docstring is
explicit: *"Markdown chunks ride the symbols table (kind MarkdownDoc) so the
content-hash incremental skip covers them without extra plumbing."* They are
**also** in LanceDB (`symbol_type="markdown"`, `pipeline.py:468-482`), but the
graph row is the load-bearing one here.

Consequence: a decision **is** a first-class `symbols` row today. `graph.symbol(
"docs/…::anchor")` resolves it. Therefore a `GOVERNS` relation can be an ordinary
`edges` row (`src = decision qn`, `dst = governed code fqn`, `kind = "GOVERNS"`),
and "what decisions govern this symbol?" is a **direct clone of `callers()`**
(`graph_store.py:278-299`) with `kind='CALLS'` swapped for `'GOVERNS'`. No
parallel store, no property-graph, no DDL. (A caution for future readers: the
retrieval *hydrate* path (`hybrid.py:162-174`) takes a `dense_by_qn` fallback for
markdown qns yielding `kind="unknown"` — that is a search-result quirk, **not**
evidence that markdown is absent from the graph. It is present.)

Below, each of the spec's six open decisions (DK1–DK6) is committed with its
rationale and the alternative rejected.

### DK1 — Representation: overlay `GOVERNS` edges, do **not** reclassify the kind

**Decision.** Decisions stay `MarkdownDoc` symbol rows — we do **not** mutate
their `kind` to `"Decision"`. The typed layer is expressed entirely as (a) a new
`GOVERNS` **edge kind** rooted at decision chunk qns, and (b) a deterministic
**decision-bearing predicate** (DK5) that selects *which* chunks get linked. A
chunk "is a decision" iff it sits in a decision source path and linkage yields ≥1
governed code symbol. Both `symbols.kind` and `edges.kind` are free-form `TEXT`
with no enum/FK/CHECK (`graph_store.py:38,58`; the module docstring states "No FK
constraints: edge endpoints may dangle"), so `GOVERNS` needs **zero DDL** — AC 1.

**Why not reclassify `kind="MarkdownDoc"` → `"Decision"` in place?** Because
`_index_markdown` re-upserts every *changed* markdown chunk with the literal
`_MARKDOWN_KIND` on every run (`pipeline.py:466`). A kind we stamp in
`_index_decisions` would be silently clobbered back to `"MarkdownDoc"` the next
time the source doc is edited — a phase-ordering race. Overlaying edges avoids
mutating a row another phase owns. It also means decisions inherit the markdown
rows' **id stability and content-hash incremental lifecycle for free** — we build
nothing to keep the node itself fresh; we only own the edges.

**Identity.** The canonical decision id is the chunk qn `path::anchor`
(`markdown.py:84`), exactly as the spec fixed. The `<slug>` is a grouping label,
not the id. `status` (accepted/superseded) is **not persisted in slice 1** — no
column exists and supersession authoring is out of scope; how it lands without
DDL (a `kind` suffix vs. a sidecar) is a slice-2 design-surface item, not decided
here.

### DK2 — Linkage extraction: deterministic backtick-reference resolution (the crux)

**Decision.** `_index_decisions` extracts, from each decision chunk's body
(`symbols.source_code`), its **inline-code spans** (`` `…` ``) and resolves each
to a governed code symbol by two deterministic tiers:

1. **Exact fqn** — the span equals a symbol `qualified_name` **of a non-markdown
   kind**: link (`graph.symbol(span)` is a PK hit, `graph_store.py:251-274`;
   `MarkdownDoc` hits are excluded, so a `path::anchor`-shaped span never links a
   decision to another doc chunk). Highest precision.
2. **Unambiguous, code-shaped short name** — the span equals exactly **one** code
   symbol's `name` (excluding `MarkdownDoc` rows) **and looks like a code
   identifier**, not an English word: link. Needs a new `symbols_by_name(name)`
   store method (`SELECT qualified_name, kind FROM symbols WHERE name = ? AND kind
   != 'MarkdownDoc'`) — the `name` column exists (`graph_store.py:39`) but has no
   by-name lookup today (the store's only symbol getter is exact-PK `symbol()`),
   and it is unindexed, so tier-2 is a `name`-scan per fenced span; at slice-1
   corpus scale (one repo's symbols) that is acceptable, and whether
   `symbols.name` warrants an index is a build-time call, not a blocker.

The "code-shaped" guard on tier 2 is deterministic and load-bearing for AC 4: a
fenced *English word* that happens to match exactly one symbol (`` `run` ``,
`` `build` ``, `` `main` ``, `` `config` ``) is **not** ambiguous and would
otherwise be a coincidental false positive. Tier 2 therefore requires the span to
carry an identifier shape a prose word lacks — a namespace/path separator
(`.`, `::`, `/`), an internal underscore, or internal CamelCase — and drops bare
lowercase dictionary tokens even on a unique match. Anything ambiguous (a bare
`get` matching many symbols), non-code-shaped, or resolving to no symbol (a file
path like `pipeline.py`, a shell snippet) is **dropped** — never linked. The
`dst` is always a **code** fqn (both tiers exclude `MarkdownDoc`), never the
decision's own markdown chunk.

**Why this clears the AC 4 precision floor.** We only link *explicit* code
references, only when resolution is unambiguous, and only for code-shaped
identifiers — so we cannot link "obviously-unrelated" symbols, cannot be fooled
by a coincidental English-word match, and structurally cannot "trivially link
only its own doc." Grounding confirms the signal is real, not hopeful: this
repo's own decision prose is dense with fenced symbol refs — the committed
`compound-engineering-bridge.design/approach.md` names `_resolve_current_contract`,
`PREDICATES`, and `signals/predicates.py` inline. Recall is deliberately partial
(fenced refs only); an LM extraction stage for the un-fenced remainder is **out
of scope** (spec: "LLM-inferred links … later, separately-gated, off by default").

**Why not `scope.touches` globs → symbols?** They are the obvious recall boost,
but `run_index_job` takes **no contract and no diff** — its symbol-scoping inputs
are `repo_path, slug, force, index_markdown` (plus infra: `settings, embed_client,
jobs, job_id, progress_cb`) (`pipeline.py:211-222`); the only git touch is
`rev-parse HEAD`. Feeding `scope.touches` would plumb `contracts.py` into
`code_index/ingest` (a layer coupling) *and* hit the self-hosting path problem
(DK5). Kept as a design-surface recall note, not a slice-1 dependency.

### DK3 — Query: a new `governing_decisions` structural query, cloning `callers`

**Decision.** Add a store method `governing_decisions(fqn)` — the `callers()` SQL
with `e.kind = 'GOVERNS'` and reading `e.src` (the decision), `LEFT JOIN symbols s
ON s.qualified_name = e.src` to hydrate the decision row. Expose it by extending
`_STRUCTURAL_QUERIES` and `_FQN_QUERIES` (`search_router.py:22-23`) with a
`governing_decisions` key and a branch in `search_structural._run`
(`search_router.py:141-152`). The `(kind, dst)` index (`idx_edges_dst`,
`graph_store.py:67`) already covers the predicate shape — no new index. The
traversal is deliberately **one hop** (a decision directly governing the symbol),
not transitive: unlike `transitive_callers`, "governs" has no useful closure — a
decision about `foo` does not govern everything `foo` calls. Closes AC 5.

**Why reuse `_STRUCTURAL_QUERIES`, not a bespoke `/knowledge` route?** Because the
traversal shape *is* `callers()` with a different edge kind, and `idx_edges_dst`
already covers it — a dedicated route would duplicate the dispatch, validation
(400-on-missing-`fqn`), and handle-management (`with_handles`) that the structural
router already gives for free (`search_router.py:133-152`). The decision node's
*shape* differs (DK4's `DecisionView`), but the *query* does not; overloading the
existing rail costs nothing and keeps one query surface. The distinct *CLI*
namespace that preserves module identity is DK7's job, not the route's.

### DK4 — Decision result view (not `CallSiteView`)

**Decision.** The structural branch returns a new `DecisionView`
(`api/models.py`) — `qualified_name`, `file_path`, `start_line`, `heading`,
`snippet` — **not** `CallSiteView`. `CallSiteView` is `{qualified_name, file_path,
line}` where `line` is a concrete *call-site* line; for a decision that field is a
heading offset and the fields that matter (the heading text = `symbols.name`, the
body = `symbols.source_code`) have no home in it. The semantic `SearchResult`
shape (`hybrid.py:110-119`) is the closer template. This resolves the spec's
"query-result view … `line` is meaningless for a decision" surface item.

### DK5 — Sources: filter existing rows by path, no new walk, both `approach.md` shapes

**Decision.** `_index_decisions` does **not** re-walk the repo. Markdown discovery
is hard-coded to `rglob("*.md")` minus `EXCLUDED_DIRS`, and `docs/` is **not**
excluded (`markdown.py:23-35,56-72`; guarded by
`tests/test_codify_guards.py:45-46`) — so **all** `docs/**/*.md` are *already*
`MarkdownDoc` rows. The phase selects **decision-source chunks** from those rows
by a **configurable path allow-list**, defaulting to the three shapes that
actually exist on disk:

- `docs/solutions/*.md` — the #375 codify target *(none on disk yet; it is the
  future qa output — a hard-coded scanner keyed only on it would find nothing
  today, so it must be one entry among several, not the sole source)*.
- `docs/design/*/approach.md` — the live convention (3 such dirs exist today).
- `docs/spec-contracts/*.design/approach.md` — the **newer** convention agentalloy
  self-hosts under. **Both** approach shapes are real; a scan restricted to
  `docs/design/*/approach.md` would miss every committed spec-contracts copy —
  the exact self-hosting blind spot the spec flagged.

**Excluded by default (resolving the spec's ship/qa must-answer):** `docs/ship/*.md`
and `docs/qa/*.md` are **not** in the default allow-list. Those phases emit
delivery/PR-narrative and verification-report prose, not decision rationale — the
"why we chose X over Y" lives in the design `approach.md` and the qa `solutions`
lesson, which are the two the codify gate and design phase actually write. Ship/qa
remain **addable** through the same configurable seam if a repo puts rationale
there; they are excluded by default, not by inability.

Within an allowed file, candidacy is **path-scope + linkage-nonempty**: every
heading-chunk in a source file is a candidate, and a candidate *becomes* a
decision iff DK2 linkage yields ≥1 governed symbol. This deliberately avoids a
brittle heading-keyword predicate — grounding showed the `### D# —` marker is
**not** universal (older `approach.md`s use `## Decisions`), so keying on it would
silently miss decisions. An optional heading-keyword refinement (skip a pure
`## Problem`/`## Context` chunk that merely mentions a symbol) is a build-time
tuning detail, noted for the test-plan, not a gate. The allow-list is a small
config constant seam (there is none today — `discover_markdown_files` takes only
`repo_root`), which also answers the spec's "resolve the real, configurable
source glob(s)."

### DK6 — Edge lifecycle: `file_path`-on-doc + **doc-granular** re-derive, for AC 3

**Decision.** Every `GOVERNS` edge carries `file_path = the decision doc's path`
(not the governed code file), and `_index_decisions` re-derives at **doc
granularity, not chunk granularity** — the design's load-bearing correction. It
runs **after** `_index_markdown`, consumes that run's `changed`/`removed` sets
(which `_index_markdown` must **return** — it yields only a count today), and
computes the **affected docs** = every allow-listed source path with ≥1 chunk in
`changed ∪ removed`. For each affected doc it (1) deletes *all* that doc's
`GOVERNS` edges (`DELETE FROM edges WHERE kind='GOVERNS' AND file_path = ?`) and
(2) re-derives edges for **every current decision chunk of that doc**. A doc with
no chunk in `changed ∪ removed` is left entirely untouched.

**Why doc-granular, not the per-changed-chunk delete-by-src first sketched.** The
pruning primitive `_index_markdown` already uses, `delete_for_files(doc)`, deletes
edges by `file_path IN (…)` **regardless of src or kind** (`graph_store.py:226-247`)
— it is doc-granular. If a doc drops chunk A while an *unchanged* decision chunk B
survives, `delete_for_files(doc)` wipes **B's** edges too (same `file_path`); a
re-derive keyed only on the `changed`/`removed` chunks would never revisit B, so
B's links would vanish though B never changed — an AC 3 violation. Matching the
re-derive's granularity to the delete primitive's removes that mismatch. The
lifecycle then holds cleanly:

- **Unrelated code re-index** → `delete_for_files(code files)`; our edges carry
  `file_path = doc`, untouched → links **survive** (AC 3, first half).
- **Doc content-unchanged** → not an affected doc → nothing runs → **survive**.
- **Doc edited (chunks changed, none removed)** → `_index_markdown` does
  `INSERT OR REPLACE` and does **not** call `delete_for_files`; the doc is
  affected, so we delete all its `GOVERNS` edges and re-derive every current
  decision chunk → **stale links pruned, survivors preserved** (AC 3, second half).
- **Doc chunk removed** → `_index_markdown` calls `delete_for_files(doc)` (dropping
  every `GOVERNS` edge of the doc, siblings included); the doc is affected, so we
  re-derive every *surviving* decision chunk → the removed chunk's links are gone,
  the siblings' links **restored** in the same pass. No sibling collateral.

**Known slice-1 gap — staleness frozen at doc-index time (accepted, out of
scope).** Because re-derivation is triggered only by a *doc* change, two *code*-side
churns are not re-evaluated until the doc next changes: (a) a governed symbol is
**renamed**, so its `dst` fqn dangles (the spec explicitly defers symbol-rename
tracking); and (b) a DK2 tier-2 link that was **unambiguous at index time** becomes
ambiguous when a second same-named symbol is later added — the once-unique
resolution is never revisited. Both are the same family of false-negative/stale-`dst`
and both ride the out-of-scope rename-tracking deferral; slice 1 ships
create/prune-on-**doc**-change only. Surfaced in the test-plan as an accepted gap,
not a failing case.

### DK7 — Pull verb: `agentalloy knowledge why <symbol>` (a distinct CLI namespace)

**Decision.** The AC 7 pull front door is `agentalloy knowledge why <symbol>`
(alias `knowledge for <path>`), a **new top-level `knowledge` subparser group**
registered like every other subcommand module (`install/__main__.py` import +
`_SUBCOMMANDS`). Its handler is a thin httpx client — the exact `_run_structural`
pattern the `code` verbs use (`install/subcommands/code.py`) — that GETs the
shared `/code/search/structural?query=governing_decisions&fqn=…` route (DK3) and
prints one decision per line. The HTTP surface is shared with Code (decisions are
a typed overlay on the same store); the **CLI namespace is distinct**.

**Why not `agentalloy code why` under the existing `code` group?** It is the
smaller diff, but it buries a *Knowledge*-module verb inside the *Code*
subcommand, directly undercutting the spec's Boundaries section ("what keeps
Knowledge distinct") and AC 9's module-identity line, and it silently renames the
spec's own `knowledge why` (AC 7, concern 4). The module boundary is exactly what
this feature is establishing, so a few extra lines to register a `knowledge` group
is the right trade — the boundary shows up where users actually meet it, on the
command line. Sharing the *route* while splitting the *namespace* keeps one query
surface without conflating the two modules.

## What slice 1 does **not** do (carried from spec)

- **No injection (AC 6).** No `signals/`, no `proxy_injection.py`, no push at
  design/build. That is slice 2, and with it the superseded-exclusion and the
  Instructions-deferral (a promoted skill already front-loads its `rationale`).
- **No supersession authoring, no `status` persistence, no rename re-resolution,
  no semantic decisions-only `search_similar` kind-filter** (AC 8 already keeps
  decision text retrievable via `code search`; a `symbol_type` filter on
  `search_similar` is a latent enhancement, not a slice-1 AC — deferred to keep
  the slice tag-focused).
- **No engine or corpus change.** `code_index/engine/` (vendored, and the source
  of the only cloud-provider *names* in the tree — `engine/constants.py`, inert,
  not in the live path) and `_corpus/` are untouched; AC 8's no-network assertion
  guards against importing those provider enums in the live decision path and
  pins the embed base_url to the local llama-server (`config.py:90`,
  `embed_provider.py`).

## Boundaries preserved (AC 9)

Decision types live **only** in the code-index store (`symbols`/`edges`); no
decision is auto-installed as a corpus skill (that is the separate #375 *promote*
path, an Instructions artifact). The anti-double-inject deferral to Instructions
is an AC 6 rule and rides slice 2 — slice 1 asserts only the structural boundary
(no corpus write from the decision path).
</content>
