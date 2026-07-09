# Compound Engineering ↔ AgentAlloy Bridge — Design

> Runtime home: `docs/design/compound-engineering-bridge/approach.md` (git-ignored).
> This is the committed copy. Acceptance lives in
> `docs/spec/compound-engineering-bridge.md` (`## Acceptance Criteria`, AC 1–8)
> and is not reopened here.

## Approach

The feature is two subsystems joined only by a shared file convention
(`docs/solutions/<slug>.md`). They ship independently and touch disjoint code, so
the design keeps them decoupled: Piece 1 is a **gate + prose** change on the ship
workflow skill; Piece 2 is a **new CLI subcommand** that reuses the pack rail.
Nothing here touches `code_index/`, `retrieval/`, or `api/` (AC 6) — the
read-path is inherited.

Below, each of the five decisions the spec deferred is committed with its
rationale and the alternative rejected.

### D1 — Codify-gate predicate: a new slug-scoped `lessons_recorded`

**Decision.** Add one new deterministic predicate, `lessons_recorded`, to
`signals/predicates.py`, registered in `PREDICATES`. It resolves the **active
task slug** via the runtime's canonical work-item resolver — **not** by mtime —
and returns `MET` iff `docs/solutions/<slug>.md` exists, `NOT_MET` if it does
not, and `UNKNOWN` when no single work-item can be resolved. It is DB-free (pure
disk reads), so it needs no re-embed and takes effect the moment the wheel
carries it.

**Why not `artifact_exists`?** Ruled out by AC 2: `artifact_exists:
docs/solutions/*.md` is `MET` on *any* match, so the first lesson ever written
satisfies it forever — the stale-file no-op.

**Why not `artifact_newer_than`?** Write-order-fragile: if the agent writes the
lesson before the phase's exit artifact in the same phase, the mtime comparison
falsely reports `NOT_MET`. A slug-scoped existence check is order-independent.

**Slug resolution (spike D1 — resolved).** The canonical resolver is
`_resolve_current_contract(cwd, phase)` (`api/proxy_signal.py:164`):
cursor-first (`.agentalloy/cursor`, written by `agentalloy task next`), then the
sole `contracts/<phase>/*.md` if exactly one exists, else — for an uncursored
fan-out (≥2) — the newest by mtime (per PR #376: a tag-scoped compose of the
most-recent work-item beats a filler-leaking free-text fallback), and `(None,
None)` only when zero contracts exist. `lessons_recorded` reuses it against
`ctx.current_phase` and takes `Path(...).stem` as the slug; `(None, None)` →
`UNKNOWN`. (The codify gate lives on qa, a single-contract phase, so the fan-out
branch never applies to it; and slug-scoping means AC 2 holds regardless of which
contract is resolved.) `latest_contract` (mtime, `contracts.py:387`) is a
footgun here — it ignores the cursor, disagrees with `task next`'s filename
ordering, and can select a stale prior-cycle contract (nothing deletes old
contracts; only the cursor is cleared on transition).

> **Amended by D6 (post-merge).** The `origin/main` merge relaxed the resolver to
> the newest-by-mtime fan-out fallback described above, which — shared with this
> gate — could block against an mtime-*guessed* slug. D6 resolves this: the
> resolver is **cursor-strict** again and the cursor is **seeded on phase entry**,
> so `lessons_recorded` resolves the seeded work-item and only a genuinely
> uncursored fan-out fails open (`UNKNOWN`). See D6 below.

**Layering fix (required, folded into task 01).** `_resolve_current_contract`
lives in the `api` layer, but predicates live in `signals`, and `signals` must
not import `api`. Factor the resolver down into `contracts.py` (or
`skill_loader.py`) and have both the proxy and the predicate call it there.

**Prose↔gate coupling (AC 3).** Because the predicate is slug-scoped it need not
expose a `path` glob, so — unlike the original `artifact_exists` sketch — it does
not *auto*-derive the `docs/solutions/` prose token. Coupling is instead made
explicit in the host skill: the `docs/solutions/<slug>.md` write instruction goes
in `raw_prose`, and the `docs/solutions/` token is asserted as an invariant (an
authored `prose_invariants` entry, or the predicate's optional advisory `path`
arg — build's choice). Either way prose and gate stay consistent, and the
'write the lesson' advisory can still fire.

### D2 — Gate the `qa → ship` forward edge (not ship close-out)

**Decision (spike D2 — resolved; overrides the spec's tentative ship-close-out
option).** Append the `lessons_recorded` leaf to **`sdd-verify-and-review.yaml`**
(the `qa` phase), whose forward edge is `qa → ship`. A `NOT_MET` leaf there
blocks `agentalloy phase set ship` until the lesson exists.

**Why not ship close-out, as first imagined?** Because it is *unenforceable*.
Ship is terminal and self-loops (`_PHASE_GRAPH["ship"] = "ship"`, gates.py:41),
and the only route out is the user-initiated reset `phase set intake`. Both the
CLI completeness gate and the approval gate short-circuit on `target !=
_PHASE_GRAPH[current]` (`phase.py:116, 159`), so the `ship → intake` reset — like
every backward/bail/reset jump — is **never gated**, and ship's `exit_gates` are
effectively never enforced on any transition. A codify leaf on ship would be
inert. `qa → ship` is a real forward edge (`_PHASE_GRAPH["qa"] = "ship"`,
gates.py:35) and does evaluate qa's `exit_gates` via `_forward_gate_blocks`.

**Consequence for the design.** Codify moves from "ship close-out" to "the last
gate of qa, just before ship" — still after the change is verified and before it
is delivered, faithful to compound engineering's compound-last step (and
slightly better: the lesson exists before ship writes its PR narrative). The
prose change and the leaf both land in the **qa** skill; the slug the predicate
resolves is qa's current work-item — the same `task_slug` that chains into ship,
so `docs/solutions/<slug>.md` is unchanged.

**Determinism caveat.** `_forward_gate_blocks` evaluates with `lm_client=None`,
so only a hard `NOT_MET` blocks — an `UNKNOWN` passes. `lessons_recorded` is a
deterministic disk check, so it blocks correctly; it must never depend on an
embed/LM path.

### D3 — `--force` bypasses codify (no carve-out)

**Decision.** `lessons_recorded` is an ordinary forward-gate completeness leaf.
`agentalloy phase set --force` bypasses it, as it does the other completeness
leaves — an intentional, logged escape hatch. We do **not** add an unconditional
carve-out like the #10 approval gate's `_approval_gate_blocks`.

**Why.** Proportionality. The approval gate guards a *human sign-off* that
`--force` must not silently skip; codify is a completeness obligation, not a
trust boundary. No AC requires `--force` survival. Adding a carve-out would
over-constrain the escape hatch users rely on when they legitimately need to
move on.

### D4 — Promotion is a new first-class CLI subcommand

**Decision.** Add `agentalloy lessons promote <slug>` (module
`src/agentalloy/install/subcommands/lessons.py`), with a helper generator module
that turns `docs/solutions/<slug>.md` into a domain-skill pack under
`.agentalloy/custom-skills/<slug>-lesson/`, then installs it by calling the
**existing** `install_local_pack` code path (strict mode, dedup on). The
lesson→fragment mapping is the spec's table: approach→`execution`,
verification→`verification`, decision/what-didn't-work→`rationale`; the lesson's
module/problem tags → `domain_tags` (clamped to the domain tier's soft ceiling).

**Why not the interactive `add-skill` lane?** That lane is an agent-driven,
per-skill human-authoring session gated by `agentalloy approve add-skill`.
Promotion is a *batch transform* of an existing artifact — it should be
scriptable and composable (CI, a post-ship hook), which a CLI subcommand is and
the interactive lane is not. The subcommand still reuses the lane's install rail
and its custom-skills location, so nothing is reimplemented.

### D5 — Duplicate handling: pre-ingest similarity probe

**Decision.** Before installing, the subcommand **embeds the candidate fragments
and runs the dedup classifier against the corpus** (`dedup_gate.classify_hit`,
hard ≥ 0.92 / soft ≥ 0.80). On a hard hit it **refuses the promotion** and names
the near-duplicate skill; on a soft hit it warns and proceeds; `--allow-duplicates`
downgrades a hard hit to a warning. Only after the probe passes does it call the
install rail.

**Why a pre-ingest probe rather than post-`EXIT_DEDUP` rollback?** The spec's
fact-check established that `install-pack` writes the skill rows and vectors
*before* the dedup pass runs and never rolls back — `EXIT_DEDUP` is only an exit
code. Prevention beats cleanup: probing first means a hard duplicate is never
written, so AC 5's "not left served in the corpus" holds without inventing an
uninstall path (which doesn't exist today). The probe reuses the same embed
model and classifier the rail uses, so its verdict matches what the rail would
have reported.

### D6 — Post-merge: cursor is the single source of truth (Outcome B)

**Decision (post-merge reconciliation with #376/#377).** When this branch merged
`origin/main`, the shared `resolve_current_contract` was reconciled to #376's
newest-by-mtime fan-out fallback — but D1's gate and predicate were authored for a
**cursor-strict** resolver. The two callers then disagreed: the proxy wanted a
best-effort guess (avoid free-text filler), the gate needed strictness (never
block on a guess). Left as-is, the qa→ship gate could block against an
mtime-guessed slug whenever `contracts/qa/` held ≥2 uncursored contracts, and
mtime is fragile (git checkout/clone reset it).

Resolved by making the **cursor the single source of truth**, reliably set:

- **Seed on phase entry.** `_write_phase_atomic` (proxy) and `run_phase_set` (CLI)
  now seed `.agentalloy/cursor` to the new phase's first work-item by filename
  order (`contracts.first_workitem_id`, shared with `task.py`'s ordering) instead
  of clearing it. A phase with no contracts still clears. `task next` advances it.
- **Resolver goes strict.** `resolve_current_contract`: cursor → single-item →
  fan-out `(None, None)`. With seeding, the fan-out floor is rarely reached; when
  it is, the proxy composes nothing and the gate fails open (`UNKNOWN`) — neither
  guesses.

Both consumers now read one cursor. #376's goal (no free-text filler) is met more
robustly — a seeded cursor means composition is always tag-scoped, not
mtime-guessed — and this supersedes #376's mtime fallback. Verified via the
`test_phase_transition_seeds_*` and `test_tier2_*`/`test_ambiguous_fanout_is_unknown`
suites and a live `phase set` → seeded-cursor CLI check.

## Non-goals carried from spec

No change to `code_index/`, `retrieval/`, `api/`, or the pre-seeded corpus; no
automatic promotion; injection of a promoted skill is inherited from the
existing install→corpus→signal rail and not re-implemented or re-tested here
(AC 6). See `docs/spec/compound-engineering-bridge.md` `## Out of Scope`.
