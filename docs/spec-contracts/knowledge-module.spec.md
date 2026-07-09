# Knowledge Module — Architecture Spec

> **Scope in a sentence.** Build AgentAlloy's third context module — **Knowledge**:
> the decisions behind the code and *why* they were made — as a typed layer over
> the existing code index. A decision record links to the symbols it governs; the
> module *indexes and serves* the "why" already captured across the SDD lifecycle
> (adding no new authoring ritual beyond the shipped codify gate), and surfaces it
> just-in-time when an agent touches governed code.

This is the module-level architecture spec. Delivery is **sliced** (§ Delivery
slices); a capture input already shipped in #375. Acceptance is stated per the
whole module; design fans it into per-slice work. The `## Architecture` section
is **grounding, not binding** — mechanisms named there justify feasibility; the
Acceptance Criteria are behavioral and the open "how" lives in `## Design surface`.

## Context

AgentAlloy ships two context modules today and names a third on the roadmap
(`README.md`):

- **Instructions** — knows *how you work* (the signal layer + skill corpus).
- **Code** — knows *what's there* (the symbol graph + hybrid search).
- **Knowledge** *(this spec)* — knows *why it's that way*: the decisions, the
  rationale, and the alternatives rejected — linked to the code they govern.

The gap Knowledge fills: an agent editing a function can already ask "what calls
this?" (Code) and "how do we work here?" (Instructions), but not "**why** is it
this way — what decision made it so, and what did we already reject?" That "why"
is the most expensive thing to reconstruct and the easiest to violate.

**The module composes existing capture; it adds no new authoring ritual.** The
"why" is written by the SDD lifecycle *as it runs* — the table below lists the
phases' output **conventions** (what each phase produces per task), not files
already present in every repo (a fresh repo has none until the lifecycle runs):

| Source | Path / id | What it holds |
| --- | --- | --- |
| Per-task lesson | `docs/solutions/<slug>.md` | problem, approach that worked, what didn't, decision worth keeping (shipped #375) |
| Design rationale | `docs/design/<slug>/approach.md` `## Approach` | the decisions + the alternatives rejected |
| Retrieval-level "why" | `rationale` fragment type (`ingest.py`) | the corpus channel for why-queries (lint R8) |
| Decision methodology | `design-review-rfc-and-adrs` skill (live) | Nygard ADR + RFC formats |

All `docs/*.md` are **already ingested** by the code index (markdown discovery
excludes only `EXCLUDED_DIRS`), so decision text is retrievable *the moment it's
written*. Knowledge's job is to make it **typed, linked, deduped against
Instructions, and injected** — not to add a fourth place to write things down.
The one capture obligation that exists — the qa codify gate writing
`docs/solutions/<slug>.md` — shipped in #375; Knowledge treats it as an *input*,
not something it re-adds.

## Architecture *(grounding — not binding on acceptance)*

Five concerns, cleanly split write-side (1–3) from read-side (4–5). Each names
what is *already supported* (reuse) vs. *new work*, grounded in the code so
design starts from reality.

### 1. The Decision — the unit and its identity

**What.** A `Decision` is a durable record with: an id, a title, the
context/problem, the decision, the alternatives rejected, its consequences, a
status (`accepted` | `superseded`) with supersession links, and — the
differentiator — the set of **code symbols it governs**.

**Identity & granularity (resolved here, because it determines edge
granularity).** A decision is a **decision-bearing heading-chunk** of a source
doc, not a whole file — a single `approach.md` `## Approach` or a solutions file
routinely holds several decisions, and the markdown ingest already chunks by
heading. The canonical id is the markdown chunk id `path::anchor`; the task
`<slug>` is a *grouping label*, not a unique decision id. Supersession links
therefore reference `path::anchor`, which is stable across edits within a file.

**Disambiguation (which chunks are decisions).** Not every markdown chunk is a
decision — a solutions file has Problem / Approach / What-didn't-work sections.
Only the **decision/rationale-bearing** sections qualify (the `## Approach` of
`approach.md`; the "approach that worked" / "what didn't work" / "decision worth
keeping" of a lesson). The exact predicate is a design detail; the *rule* — index
decision-bearing sections, not all markdown — is fixed here.

**Representation (reuse).** The code index's `symbols`/`edges` tables use
free-form `TEXT` `kind` columns with no enum/FK and are "rebuilt not migrated",
so a typed decision layer is addable with **no schema migration** (whether by
reclassifying the existing markdown rows or a parallel typed layer is a design
choice — see Design surface).

**Supersession is represented but authored later.** The `status` field and
supersession links are part of the unit so injection can *filter* them (concern
5), but *producing* them (detecting that a newer decision overrides an older one)
is deferred to a later slice / design (see Out of Scope + Design surface). What
ships with injection is the **guard**, not the authoring.

### 2. Capture — compose the lifecycle's existing "why"

**What.** Knowledge's authoritative decision sources are the existing
`docs/solutions/*.md` and `docs/design/*/approach.md` — already produced and
gated by the SDD lifecycle. No new authoring ritual, no new file convention.

**Explicit non-adoption:** `docs/architecture-decisions/` and `CLAUDE.md` (the
external compound-engineering convention) are **not** sources — consistent with
#375's decision to consolidate per-task "why" into `docs/solutions/`. Corollary:
a decision authored via the `design-review-rfc-and-adrs` skill is only indexed if
it lands in `docs/solutions/` or an `approach.md` — the ADR *format* is reused,
its canonical `docs/architecture-decisions/` *location* is not.

### 3. Linkage — decision → governed symbols (the core new work)

**What.** A new deterministic ingest phase extracts, for each decision, the code
symbols it governs and links them. This is genuinely new: today **no markdown row
links to any code row** — the markdown ingest writes symbol + vector rows only,
never edges — so this is the *first* markdown-derived edge producer in the
system. It hooks into `run_index_job` as a phase modeled on `_index_markdown`,
inheriting the content-hash incremental machinery.

**Linkage must be *correct*, not merely present** — see AC 4. The extraction
method (scope globs → symbols, symbol-name mentions in the decision text, or
diff-derived) is the hardest open question, deferred to design; but its output
carries a precision floor in acceptance so "any output" cannot pass.

### 4. Retrieval & query — "what decisions govern this?"

**What.** The read *front door* (both content and by-symbol), reusing existing
surfaces:

- **By content (reuse, works today):** decision text is already semantically and
  lexically searchable; a kind-filter (new) would allow decisions-only queries.
- **By symbol (new):** a structural query "what decisions govern `fqn`?", the
  same shape as the existing `callers` traversal. A pull verb —
  `agentalloy knowledge why <symbol>` / `knowledge for <path>` — is this query's
  CLI front door (the "the agent asks, nothing is pushed" model Code already uses).

### 5. Injection — surface the why just-in-time (composed push only)

**What.** The distinct value beyond concern 4's pull: at **design/build**, when a
governed symbol falls in the task's scope (the task contract's `scope.touches`
resolving to a governed symbol), the signal layer *front-loads* the governing
decision's why — the agent doesn't have to know to ask. Reuses the existing
composition path; adds no new proxy *surface*; never touches the prompt-cached
system block. Two hard rules:

- **Never surface a `superseded` decision** — a stale, overridden rationale is
  worse than silence for a "why" module.
- **Defer to Instructions for a promoted decision.** When a decision has already
  been promoted to a domain skill (the #375 `lessons promote` path), the signal
  layer *already* front-loads its `rationale` fragment for matching phase/tags —
  Knowledge must not double-inject the same why. Instructions owns the JIT surface
  for promoted decisions; Knowledge owns it for the not-(yet-)promoted ones.

### Boundaries (what keeps Knowledge distinct)

- vs. **Code**: Code is structure (symbols + CALLS); Knowledge is *why*
  (decisions + a governs-relation), a typed overlay on the same store — not a
  fork of it.
- vs. the **ADR skill**: `design-review-rfc-and-adrs` teaches *how to write* a
  decision; Knowledge *indexes and serves* the decisions written (that land in
  its sources). It consumes the skill's format, it doesn't replace it.
- vs. **Instructions**: a domain skill is reusable *how-to*; a decision is a
  *this-repo, this-symbol* fact. A recurring decision may be *promoted* to a skill
  (the #375 bridge). Once promoted, **Instructions owns its JIT surfacing** and
  Knowledge defers (concern 5) — so the two never double-inject the same why.

## Delivery slices

Each slice's value is stated honestly (some are user-observable; the linkage
foundation is test-observable).

- **Slice 0 — Codify capture *(SHIPPED, #375)*.** The qa codify gate writes
  `docs/solutions/<slug>.md`. This is Knowledge's *capture input*. (The #375
  *promote* half produces a **domain skill** — that is an *Instructions*
  artifact, not part of this module; it's the bridge, not a Knowledge slice.)
- **Slice 1 — Type, link & query.** The `_index_decisions` phase (typed decisions
  + governs-links from the sources) **plus** the structural query and the
  `knowledge why` verb — bundled so the slice is *user-observable* (you can ask
  "what decisions govern this symbol?"), not a dead data layer. Closes AC 1–5, 7–9.
- **Slice 2 — JIT composed injection.** The design/build push, superseded-
  suppressed and Instructions-deferred. Closes AC 6.

## Acceptance Criteria

1. **Typed decisions with zero schema migration.** A decision and the symbols it
   governs can be written and read back from the code-index store with **no
   DDL/ALTER** change. Verifiable by a unit test that ingests a decision source
   and reads back the decision and its governed symbols. *(Row/edge shape is
   design's choice — see Design surface.)*
2. **Capture is composed, not re-invented.** The decision sources are the SDD
   lifecycle's own outputs (`docs/solutions/*.md` and the design phase's
   `approach.md` — the paths the qa/design phases already write to); the feature
   adds **no new write-side authoring ritual beyond #375's codify gate**, and
   `docs/architecture-decisions/`/`CLAUDE.md` are neither read nor written.
   Verifiable by a grep/guard test.
3. **Links survive incremental re-index.** A content-unchanged re-index preserves
   a decision's governed-symbol links; editing or removing a decision source
   prunes its now-stale links. Verifiable by an incremental-reindex test.
4. **Linkage is correct, not just present (quality floor).** A fixture decision
   with a *known* governed symbol yields **that** symbol and does **not** link to
   obviously-unrelated symbols (nor trivially to only its own doc). Verifiable by
   a precision test on a fixture. *(This is the floor the deterministic extractor
   must clear; the exact method is design's.)*
5. **"What decisions govern this symbol?" is answerable.** Given a code `fqn`, a
   structural query returns the decisions governing it. Verifiable by a store
   unit test + a route/CLI test.
6. **The why is pushed just-in-time — not merely pullable.** At design/build,
   when a governed symbol is in the task's scope, the governing decision's
   rationale is **composed into context without the agent querying for it**; the
   push **excludes `superseded` decisions** and **defers when a promoted skill
   already covers the decision**. Verifiable by a composition test asserting a
   push occurs (a pull-only verb does NOT satisfy this), plus tests for the
   superseded-exclusion and promoted-skill-deferral rules.
7. **A pull query exists too.** `agentalloy knowledge why <symbol>` (or the
   equivalent route) returns the governing decisions on demand. Verifiable by a
   CLI test. *(Distinct from AC 6 — this is the pull front door, AC 6 is the push.)*
8. **No regression to the read-path or determinism.** Decision text stays
   retrievable via `agentalloy code search` with no change to `code_index/engine/`
   or the pre-seeded corpus; the index and query paths make no cloud/paid-LLM
   call. Verifiable by a guard test + a no-network assertion.
9. **Boundaries hold.** The decision types live **only** in the code-index store
   (not the skill corpus); no decision is auto-installed as a corpus skill; and
   Knowledge's push defers to Instructions when a promoted skill covers the
   decision (the anti-double-inject guard from AC 6, asserted structurally).

## Out of Scope

- **A new decision-authoring ritual or UI** beyond the shipped #375 codify gate —
  Knowledge composes the lifecycle's existing artifacts.
- **Producing supersession links** (detecting that a newer decision overrides an
  older one). Only the *injection guard* (don't surface a `superseded` decision)
  is in scope; authoring/inferring the `superseded` status is deferred.
- **Edge re-resolution across code rename/churn.** If a governed symbol is renamed
  and the link dangles, repairing it is deferred (see Design surface) — slice 1
  ships create/prune-on-doc-change only, not symbol-rename tracking.
- **A code-index engine rewrite or property-graph migration** — the flat DuckDB
  `symbols`/`edges` tables are reused as-is (free-form `kind`).
- **LLM-inferred decisions/links at index time** — linkage is deterministic first;
  an optional LM extraction stage is a later, separately-gated addition, off by
  default like the existing LM-assist.
- **`docs/architecture-decisions/` / `CLAUDE.md` as sources** — consolidated on
  `docs/solutions/` + `approach.md` per #375.
- **Cross-repo / org-wide decision graphs** — per-repo, like the code index.
- **Delivering the whole module in one change** — it ships in slices.
- **A new prompt/injection *surface*** — injection reuses the existing composition
  path and the code-index query rails; it never touches the prompt-cached system
  block.

## Design surface (hand-off to the design phase)

Open "how" decisions design must resolve; recorded so design starts grounded, not
to constrain acceptance:

- **Linkage extraction + its precision target (the crux).** How
  `_index_decisions` decides which symbols a decision governs — from the source
  contract's `scope.touches` globs → symbols, from symbol-name mentions in the
  decision text, from the accompanying commit diff, or a combination — and the
  precision/recall bar it must clear for AC 4. Must stay deterministic for slice 1.
- **Edge decay under code rename/churn.** The likeliest real-world false-negative:
  a governed symbol is renamed and the link silently dangles, so the decision
  stops surfacing on code it still governs. How (and whether, this slice) to
  re-resolve links across renames.
- **Injection de-dup vs the promoted-skill path.** The exact mechanism by which
  Knowledge's push detects that a decision already has a promoted skill and
  defers (AC 6/9) — e.g. a back-reference from the promoted skill to its source
  `docs/solutions/<slug>.md`.
- **Node/edge representation.** Reclassify the existing `MarkdownDoc` rows to a
  decision kind, or a typed parallel layer referencing them (affects incremental
  dedup and search); and the query-result view for a decision node (the existing
  structural route returns a call-site-shaped view whose `line` is meaningless for
  a decision — a decision view is likely needed).
- **`search_similar` kind-filter.** The new filter param needed for
  decisions-only semantic queries (none exists today).
- **Authoritative sources + their real path (must-resolve).** Whether
  `docs/ship/*.md` and `docs/qa/*.md` also feed decisions, or only `solutions` +
  `approach.md`. And critically: the extraction phase must target the path the
  repo *actually* writes. The lifecycle convention is `docs/solutions/<slug>.md`
  and `docs/design/<slug>/approach.md`, but a repo may relocate these (agentalloy
  itself currently commits its SDD design docs under
  `docs/spec-contracts/<slug>.design/`, not `docs/design/<slug>/`) — so a
  hard-coded `docs/design/*/approach.md` scan would find nothing when self-hosted.
  Resolve the real, configurable source glob(s).
- **Supersession model (when it lands).** How a decision becomes `superseded` —
  inferred (a newer decision governing the same symbols) or authored.

---

*Next step per the SDD spec phase: present this spec, get approval, then
`agentalloy approve spec` to seed the design work-item for **slice 1** (type,
link & query) at `.agentalloy/contracts/design/knowledge-module.md`. This spec is
presented and stops here — it does not advance itself.*
