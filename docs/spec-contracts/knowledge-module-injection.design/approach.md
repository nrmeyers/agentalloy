# Knowledge Module — Slice 2 (JIT Injection) — Design

> Runtime home: `docs/design/knowledge-module-injection/approach.md` (git-ignored).
> Committed copy. Acceptance lives in `docs/spec-contracts/knowledge-module.spec.md`
> (`## Acceptance Criteria`, **AC 6**; boundary **AC 9**) and is not reopened here.
> This design covers **slice 2 (the push)** and **stacks on slice 1** (PR #379):
> it consumes slice 1's `GOVERNS` edges + `governing_decisions`.

## Approach

Slice 1 made decisions **typed, linked, and pullable** (`agentalloy knowledge
why`). Slice 2 makes them **pushed**: at design/build, when the task's
`scope.touches` covers a governed symbol, the governing decision's rationale is
front-loaded into context so the agent never has to know to ask. This is the
distinct value of a "why" module over a search box — and the criterion (AC 6)
explicitly excludes a pull-only verb.

The whole design turns on one grounded seam and one grounded constraint, stated
first.

### Grounding that fixes the shape

**The seam.** The proxy already composes a per-work-item block at the Tier-2
branch of `_compose_block` (`api/proxy_apply.py:202-217`). At that point the code
holds everything slice 2 needs: `signal.phase` (the design/build gate;
`_VALID_PHASES` includes both — `proxy_apply.py:40,154`), the **fully parsed
`Contract`** (`parse_contract(...)`, `proxy_apply.py:207`, so `contract.scope.
touches` is in hand — `contracts.py:47-51`), and the repo slug. The composed text
is assembled into `_ComposedBlock.text` (joined at `proxy_apply.py:223`) and
injected by `apply_signal` into the **last user message**, marker-wrapped —
**never** the prompt-cached `system` field (`proxy_injection.py:4-6,44-48`). So
slice 2 folds a decision block into that same text: it inherits AC 6's "reuses
the existing composition path, adds no new surface, never touches the cached
system block" for free.

**Load-bearing seam property — the push fires once per work-item, on entry.**
That Tier-2 branch is guarded by `if signal.announce_cursor and
signal.current_contract:` (`proxy_apply.py:202`), and `announce_cursor` is true
**only on the turn the work-item cursor changes** (`proxy_signal.py:748`,
"emitted once per work-item"); off that turn `signal.current_contract` is `None`
(`proxy_signal.py:795`) — there is no parsed contract at all. So the decision
push, sharing this seam, is a **once-per-work-item front-load at cursor entry**,
not a per-turn injection. This is deliberate and sufficient for AC 6: the "why"
is composed into context the moment the agent enters a design/build work-item
that touches governed code — front-loaded, not on-demand — and repeating it every
turn would bury the signal. DK2 makes this the first gate condition; the test
plan pins it (a non-entry turn composes no decision block, by design). If a later
need arises to re-surface on every governed-file turn, that is a *signal-layer*
change (populate `current_contract` off-entry) explicitly out of this slice.

**The constraint.** Compose and `code_index` are separate subsystems with exactly
one cross-call today — an availability *gate* (`code_index_gate`,
`proxy_apply.py:31,186`) that lazy-imports code-index code only after the
`code_index_enabled` check ("must not import while disabled",
`code_index_gate.py:19-22`). **No compose/signal code calls `governing_decisions`
today** — the only consumer is slice 1's CLI (agent-pull). Slice 2 adds the first
compose→code-index *query* cross-call, and it must obey the same lazy-import,
enabled-gated discipline.

Below, the seven slice-2 decisions, each with its rejected alternative.

### DK1 — A structural push on a parallel rail, folded into the composed text

**Decision.** The decision push is **deterministic and structural** — driven by
`GOVERNS` edges + `scope.touches`, not embedding retrieval. It is computed at the
Tier-2 seam and **appended to `_ComposedBlock.text`** as its own section,
alongside (not inside) the domain retrieval leg. It rides the existing injection
mechanism (last user message, existing phase markers) — no new marker family, no
new surface.

**Why not ride the Tier-2 *domain retrieval* leg** (`proxy_apply.py:208` →
`retrieval/domain.py`)? That leg is embedding-ranked and tag-filtered, and it is
**already** where a promoted lesson's `rationale` fragment rides
(`lesson_pack.py:138-151`, retrieved by tag match). Pushing decisions through the
same ranked leg would (a) make them compete for RRF slots against real skills and
(b) make the anti-double-inject (DK4) circular — you'd be deduping a retrieval
result against itself. A structural push, computed separately and appended, keeps
the "no LLM in the runtime path" thesis intact, and — because it is appended
**after** the domain leg composes — it can dedup against the *actually-composed*
text (DK4). The deterministic, structural shape is the same idea as the
system/applicability rail (`compose.py:393-409`, `applicability.py`), but be
precise about where it runs: that rail runs **inside** the orchestrator and is
captured by `_merge_compose_telemetry` (`proxy_apply.py:227`); the decision push
is computed **at the seam, after `orchestrator.compose(...)` returns**, so it is a
post-compose append, not a peer orchestrator leg. Consequence for observability
(DK6): the push is outside the compose telemetry merge, so it must emit its own
`pushed / decision-count / deferred / truncated` fields, not rely on the compose
trace.

**Append site (pinned, so build has one implementation, not two).** The push is
computed **inside** the `announce_cursor` branch (`proxy_apply.py:202-217`),
reusing the contract already parsed at `:207`, and its rendered block is included
as an element of the `_ComposedBlock.text` join. It runs in **its own
try/except**, independent of the domain-leg compose: a domain-compose failure must
not suppress the push, and a push failure (or an unavailable code index) must not
break the domain leg or the request — either degrades to the other's result. This
resolves the seam ambiguity (nest-in-branch vs. hoist-to-line-223): **nest in the
branch, guard independently.** It also means the push inherits the branch's
once-per-work-item cadence (the Grounding note / DK2), which is intended.

### DK2 — Fire gate: cursor-entry ∧ design/build ∧ enabled ∧ available ∧ scope

**Decision.** The push fires only when **all** hold, in this order: (0)
`signal.announce_cursor ∧ signal.current_contract` — i.e. this is the work-item's
**entry turn**, the only turn the branch runs and a parsed contract exists
(Grounding note; `proxy_apply.py:202`, `proxy_signal.py:748,795`); (1)
`signal.phase ∈ {design, build}`; (2) the code index is `enabled` **and** has a
completed index for the repo (reuse `code_index_gate`'s `code_index_available`
probe, `code_index_gate.py:36-72`); (3) `contract.scope.touches` is non-empty. Any
miss → **no push, no error** (graceful degrade). The cross-call is lazy-imported
behind the `enabled` check, exactly like the existing gate.

Condition (0) is not an extra gate we impose — it is the seam's existing
precondition, made explicit. It sets the cadence: **front-load once, on work-item
entry.** A non-entry design/build turn composes no decision block, by design (test
plan TC1b).

**Why design/build only?** The spec scopes the push there — it is where
`scope.touches` names the code the agent is about to change, so a governing
decision is actionable. At intake/spec there is no code scope; at qa/ship the work
is verification/delivery. **Why gate on availability, not just enabled?** A
decision push needs the `GOVERNS` edges, which only exist once the repo is
indexed; without an index the query returns nothing anyway, so the probe is a
cheap short-circuit that also honors the import discipline. Rejected: firing at all
phases (no actionable scope elsewhere); erroring when the index is absent (the
module must be strictly additive — a Knowledge-off or unindexed repo composes
exactly as it does today).

### DK3 — Resolution: `scope.touches` → files → decisions, via a new store join

**Decision.** Resolve the push targets in three deterministic steps:

1. `scope.touches` (globs) → concrete repo files. Reuse the **existing but
   currently unwired** `code_index_query_params` (`contracts.py:385-411`), which
   already turns `contract.scope.touches` into `path_globs` — its only callers
   today are tests, so slice 2 is its first production use.
2. files → governing decisions, via a **new store method
   `decisions_for_files(file_paths) -> list[DecisionRow]`** — one indexed join:
   `edges (kind='GOVERNS') ⋈ symbols dst ON dst.file_path ∈ files`, hydrating the
   decision `src` chunk (heading, snippet, path). This is the file-scoped analogue
   of slice 1's per-fqn `governing_decisions`.
3. glob→file matching uses `graph.list_files` (`graph_store.py:419`) + `fnmatch`,
   bounded by DK6. This enumerates the indexed file list and fnmatch-tests each —
   O(files × globs) in-memory string matching (no I/O), gated behind DK2's
   `available` probe so a cold/absent index never pays it. If profiling shows it
   matters, the alternative is a `LIKE`-lowered prefix query that caps in SQL.

**Why a join, not per-symbol `governing_decisions`?** Per-symbol would first
enumerate every symbol in every touched file, then issue one query each — O(symbols)
round-trips on the compose hot path. The single join is O(1) queries. Its edge-kind
predicate is served by the compound `idx_edges_dst`/`idx_edges_src` indexes
(`edges(kind, …)`, `graph_store.py:66-67`) and the file-side filter by
`idx_symbols_file` (`symbols(file_path)`, `graph_store.py:53`) — the edges table's
own `file_path` column is unindexed, so the join filters files on the symbol side.
Rejected: reusing `governing_decisions` per-fqn (round-trip blowup); resolving
globs in SQL (DuckDB glob-in-predicate is awkward and unindexable — resolve to a
file set first).

### DK4 — Anti-double-inject: dedup against the *actually-composed* text

**Decision.** Defer a decision **only when its promoted skill's fragment is
already present in the Tier-2 text composed this turn** — not merely when such a
skill *exists*. Because the push is appended after the domain leg composes (DK1),
`_ComposedBlock.text` (the tier-2 domain block) is in hand; for a decision sourced
from `docs/solutions/<slug>.md` (derive `<slug>` from the qn path prefix,
`path::anchor`), compute `skill_id = _sanitize_skill_id(<slug>)` (=`<slug>-lesson`,
`lesson_pack.py:69-74`, reused verbatim) and **skip the decision iff a fragment
from that `skill_id` appears in the composed domain text** (fragments render as
`### {type} — {fragment_id}`, `compose.py:412-441`, and `fragment_id` carries the
skill_id). Decisions sourced from an `approach.md` are never deferred (the #375
promote path only promotes `docs/solutions/`, so none can be covered).

**Why dedup against the composed text, not skill existence?** This is the D1 fix:
the promoted `rationale` rides the **embedding-ranked, tag-filtered** domain leg
(`retrieval/domain.py:146-160`), so a skill can *exist* yet **not** inject here —
its tags may not match this contract, or it may lose RRF. Existence-based deferral
would then step aside for an Instructions injection that never happens → the "why"
is **neither pushed nor covered** (a silent AC 6 gap). Deferring on *actual
presence in this turn's composed text* means Knowledge yields **iff** Instructions
truly covered the decision here, and pushes otherwise. As a bonus it needs **no
skill-store handle** — the helper dedups against a string it already receives
(closing the "how does the helper reach the corpus" question).

**Accepted residual — forward-collision (rare).** `_sanitize_skill_id` is lossy
(lowercase, collapse `[^a-zA-Z0-9_-]→-`, truncate-64), so two distinct source
slugs can map to one `skill_id` (`Foo_Bar.md` and `foo-bar.md` → `foo-bar-lesson`;
or two slugs differing only past char 57). If one twin is promoted **and composed
this turn**, the other's decision computes the same id, matches the present
fragment, and wrongly defers. This needs a same-turn collision of near-identical
slugs — rare — and is surfaced, not hidden; if it ever bites, the escape hatch is
to confirm the matched fragment's provenance prose (`description`/`change_summary`
carry the literal `docs/solutions/<slug>.md`, `lesson_pack.py:211,219`) before
deferring. Rejected: skill-*existence* check (the D1 silent gap); parsing
provenance prose as the primary key (brittle); a filesystem pack-dir check (can
diverge from what's served). **Known limit (out of scope):** content-level
duplication — the *same* rationale hand-written in both an `approach.md` and a
later promoted `solutions` lesson — is two different fragments and slugs, so
slug/fragment dedup cannot catch it; content-hash dedup across sources is deferred.

### DK5 — Superseded exclusion: a documented forward-compatible no-op

**Decision.** The push filters out `superseded` decisions — but this filter is,
**today, a structural no-op**, and the design says so plainly rather than
implying activity. Grounding confirmed: decisions carry **no status** — the
`symbols`/`edges` schema has no `status`/`superseded` column, `DecisionRow` has no
status field, and `_index_decisions` never sets one (the `superseded_by`/
`deprecated` fields elsewhere are a **corpus-skill** concept, unrelated to decision
docs). So no decision is ever superseded yet. The filter ships as a single guarded
predicate (`_is_superseded(decision) -> False` today) wired at the injection point,
so that when supersession authoring lands (a later, deferred slice — spec Out of
Scope) the exclusion activates with no change to the push site.

**Why include an inert filter at all?** AC 6 names superseded-exclusion as a push
rule, and a "why" module surfacing a stale, overridden rationale is worse than
silence — so the guard belongs at the seam now, correctly placed, even while its
input is always false. **Why not fake a status source** (e.g. a frontmatter read)?
That would be inventing an authoring path the spec explicitly defers, and pretending
an active filter where there is none — a data-honesty violation. The honest shape is
a placed-but-inert predicate with a test asserting it is wired (not that it excludes
anything today). Rejected: omitting the filter (AC 6 requires it, and retrofitting it
into the push site later is riskier than placing it now); implying it is active.

### DK6 — Budget: cap file resolution and injected decisions

**Decision.** The push runs on the compose hot path (the deterministic ~3000 ms
budget). Two hard caps: (a) **short-circuit** glob→file matching at
**`_MAX_TOUCH_FILES`** matches (stop scanning `list_files` once the cap is hit —
this bounds the *enumeration*, not just the result); (b) inject at most
**`_MAX_DECISIONS`** decisions, ordered deterministically (by decision path then
anchor, so the selection is stable and reviewable), **telemetry-noting** when
either cap truncates (no silent drop — see the DK1 telemetry note; the push emits
its own fields since it runs outside `_merge_compose_telemetry`). The residual
cost is the `list_files` scan up to the cap (in-memory fnmatch, gated by DK2's
`available` probe); the join is O(1) and index-served (DK3).

**Why caps, not "it's fast enough"?** A `touches: ["src/**"]` contract could
resolve thousands of files; even a cheap join over that is avoidable work on a
latency-budgeted path, and injecting dozens of decisions would bury the signal.
Rejected: no cap (unbounded fan-out); ranking by embedding relevance (that
reintroduces the LLM/retrieval dependency DK1 rejected — deterministic ordering
keeps it structural).

### DK7 — Block format: a distinct "why" section in the composed text

**Decision.** Render the decisions as their own titled section appended to
`_ComposedBlock.text` — e.g. `# Decisions governing this work`, each entry the
decision's heading, source `path`, and snippet — visually distinct from the
`# System fragments` / `# Domain fragments` sections (`compose.py:412-441`) so the
agent can tell "this is the *why*, not a how-to skill". It rides the existing
per-turn/phase markers; no new marker family (`proxy_injection.py:53-71`).

**Why a distinct section, not merged into domain fragments?** Provenance clarity:
a decision is a this-repo/this-symbol fact with a source path, not a reusable skill;
labeling it as such is what makes the push legible (and is the boundary AC 9 draws
between Knowledge and Instructions). Rejected: interleaving with domain fragments
(erases the Knowledge/Instructions distinction the module exists to make).

## What slice 2 does **not** do (carried from spec)

- **No supersession authoring** — the exclusion filter is inert until a later slice
  produces `superseded` status (DK5; spec Out of Scope).
- **No new proxy surface, no cached-system-block write** — reuses the compose text
  channel (DK1); `proxy_injection.py`'s `system`/`instructions` fields are untouched
  (in `avoids`).
- **No embedding/LLM in the push** — the selection is a deterministic structural
  query (DK1/DK6).
- **No engine/corpus change** — `code_index/engine/` and `_corpus/` untouched.
- **No slice-1 surface change** — `governing_decisions`, the route, and the CLI are
  consumed as-is; the only slice-1-area addition is the additive `decisions_for_
  files` store method (DK3).

## Boundaries preserved (AC 9)

Decisions inject only from the code-index store, never the skill corpus; the push
**defers to Instructions** whenever a promoted `<slug>-lesson` skill exists (DK4),
so the same "why" is never double-injected; and a decision is rendered as a
sourced fact, not installed or presented as a skill (DK7).
</content>
