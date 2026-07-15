# Compound Engineering ↔ AgentAlloy Bridge — Spec

> **Scope in a sentence.** Give AgentAlloy's own SDD lifecycle a compounding
> step — a lessons artifact captured at ship, a deterministic gate that enforces
> it, and a promotion path from that artifact into the instruction corpus —
> reusing the read-path (code index, signal layer) and pack rail that already
> exist, adding no new proxy or retrieval surface.

This is the spec for **Option C** of the compound-engineering coexistence
analysis: the "build the bridge" option. Options A and B (agentalloy drives
process + CE drives memory; or CE drives process + agentalloy as pure context)
need no code and are out of scope here.

> **Location note.** At runtime the SDD spec phase writes the spec to
> `docs/spec/compound-engineering-bridge.md`, which is git-ignored
> (`.gitignore` line 99). This file is the committed, reviewable copy; the
> paired spec-phase contract is `docs/spec-contracts/compound-engineering-bridge.md`.

## Context

[Compound engineering](https://every.to/guides/compound-engineering) (CE) and
AgentAlloy are complementary halves of the same idea:

- **CE is a write-path for knowledge.** Its loop ends with a *compound* step
  that writes what a task learned into files (`docs/solutions/`,
  `docs/architecture-decisions/`, `CLAUDE.md`) so the next run starts smarter.
- **AgentAlloy is a read-path for knowledge.** A signal layer just-in-time
  composes the right instructions into the agent's context at the right moment.

The seam between them is missing on the AgentAlloy side: the SDD lifecycle
(`intake → spec → design → build → qa → ship`) has **no compounding step**.
`ship` is terminal and fires nothing after delivery, and the roadmapped
**Knowledge** module ("the decisions behind the code and why they were made")
is a single sentence in `README.md` with no design behind it. This feature is a
concrete first slice of that seam.

Two independently valuable pieces, staged so each ships value alone:

1. **Codify at ship** — a lessons entry per task, plus a gate that makes the
   task un-closeable without it.
2. **Promote a lesson** — turn a lessons entry into an installed domain skill
   that the signal layer injects proactively.

The staging matters. Piece 1's output (`docs/solutions/*.md`) is **retrievable
the moment it is written** — the code index already ingests every repo `*.md`
by default (`code_index/ingest/markdown.py`; `docs/solutions/` is not in
`EXCLUDED_DIRS`), so `agentalloy code search` and `agentalloy code bundle`
surface it with zero new code. Piece 2 is the deliberate promotion of a
*recurring, proven* lesson from **passive** (queryable on demand) to **active**
(front-loaded by the signal layer for the phase and tags it matches).

## Assumptions (correct these before design)

- AgentAlloy is wired on the repo in **lifecycle-mode `full`**. The gate and the
  codify prompt only exist under `full`; `off` injects nothing and `flow free`
  pauses all workflow steering, so the bridge is correctly inert in both.
- The **source of lesson prose is CE (or the agent itself)**. This feature does
  not re-implement CE's `plan`/`work`/`review` commands or its plugin; it adds
  the capture-at-ship and the promotion path only.
- Lessons live at **`docs/solutions/<slug>.md`**, matching CE's convention and
  the code index's default markdown ingest. `<slug>` is the SDD task slug (the
  same one used for `docs/spec/<slug>.md`, `docs/ship/<slug>.md`, etc.).
- Enforcement is **cooperative-trust** — the same threat model as the #10
  approval gate (a detectable marker, a `--force` carve-out question), not
  unforgeable enforcement.

## Piece 1 — Codify at ship

**What.** Before a task's change can advance into `ship`, the agent must record
what it learned to `docs/solutions/<slug>.md`; a deterministic exit-gate leaf
makes that advance contingent on the artifact existing for *this* task. Codify is
the last gate before delivery — faithful to CE's compound-last step. (Which edge
carries the gate is a design decision — see `## Design surface`. Design resolved
it to the `qa → ship` forward edge, because ship's terminal close-out turned out
not to be gate-enforceable: the `ship → intake` reset bypasses gate evaluation.)

Two constraints were surfaced directly from the code and are load-bearing for
the acceptance below:

- **Stale-file trap.** A naive `artifact_exists: docs/solutions/*.md` leaf is MET
  by *any* matching file (`predicates.py` `_glob_files` → `root.glob`). Because
  `docs/solutions/` accumulates across tasks, the very first lesson ever written
  satisfies it forever after — turning the gate into a no-op. The gate MUST be
  satisfiable **only by a lessons entry belonging to the current task**
  (per-slug, or freshness-relative to the task). The predicate that achieves
  this is a design decision (see *Design surface*); the acceptance is stated
  behaviourally so design cannot regress into the naive form.
- **Prose ↔ gate coupling.** This is a change to the **shipped**
  `sdd-deliver-and-ship.yaml` — prose *and* gate together — **not** a profile
  prose override. Profile overrides can carry only `raw_prose` + `domain_tags`;
  `exit_gates` are always re-sourced from the shipped pack
  (`skill_loader.py` `_load_workflow_skill_for_phase`). Moreover, adding a
  `docs/solutions/…` path to `exit_gates` auto-derives a new mandatory prose
  invariant token `docs/solutions/` (`invariants.derive_invariants` →
  `_normalize_gate_path`), so the shipped `raw_prose` must literally contain that
  path, and any *pre-existing* enabled profile override for
  `sdd-deliver-and-ship` that lacks the token will be dropped at runtime (shipped
  prose served, warning logged) until its author adds it. A migration note for
  that case is in scope; auto-rewriting user overrides is not.

## Piece 2 — Promote a lesson into the corpus

**What.** A flow that reads a `docs/solutions/<slug>.md` lesson and emits a
**valid AgentAlloy domain-skill pack** under `.agentalloy/custom-skills/<pack>/`,
then installs it through the existing rail:
`agentalloy new-skill-pack` → `agentalloy validate-pack` → `agentalloy install-pack`.

Once installed, the promoted skill is front-loaded by the signal layer for the
phase and tags it matches through the existing install → corpus → signal path;
that injection is *inherited* behavior, so this feature relies on it but does
not re-test it (AC 6 pins the no-regression boundary).

The lesson's shape maps cleanly onto the domain-skill fragment taxonomy
(`ingest.py`):

| Lesson content (CE)                | Domain-skill fragment |
| ---------------------------------- | --------------------- |
| the approach that worked           | `execution`           |
| how to confirm it worked           | `verification`        |
| the decision + what *didn't* work  | `rationale`           |
| module / problem-type tags         | `domain_tags`         |

Strict-mode install (the default for `validate-pack`/`install-pack`) requires
`execution` + `verification` + `rationale` fragments and a valid production
category — one of `engineering | ops | review | design | tooling | quality`
(the seventh, `benchmark`, is reserved for benchmark packs excluded from
production retrieval and would not be used by a promoted lesson) — which is
exactly what this mapping produces.

**Curation, not just capture.** The install rail's **dedup gate** (hard cosine
≥ 0.92, soft ≥ 0.80, cross-pack) is the *signal* CE's flat
`CLAUDE.md`/lesson-file growth lacks: on a hard cross-pack duplicate it reports
the match and returns a non-zero `EXIT_DEDUP` (soft matches only warn). One
important nuance, surfaced from the code: the gate is a **signal, not a
prevention** — the existing `install-pack` rail has *already* written the skill
rows and vectors by the time the dedup pass runs, and it does **not** roll back
(`--allow-duplicates` only flips the exit code). So real curation is a
*promotion-flow* responsibility: on `EXIT_DEDUP` the flow must skip the
promotion or roll the just-installed skill back, rather than assume the rail
kept the duplicate out. CE captures; the promotion flow — acting on the dedup
signal — curates.

## Acceptance Criteria

1. **Codify gate blocks delivery.** With AgentAlloy wired (`lifecycle-mode full`),
   the lifecycle cannot advance a task into `ship` while `docs/solutions/<slug>.md`
   for that task is absent; it can once the entry exists. Verifiable by a
   gate-evaluation unit test that returns `NOT_MET` with no lessons entry and
   `MET` once present.
2. **Stale files do not satisfy the gate.** A test in which only a lessons entry
   for a *different* task (`docs/solutions/<other>.md`) exists still returns
   `NOT_MET` for the current task. (This is the explicit guard against the naive
   `artifact_exists: *.md` form.)
3. **Prose/gate self-consistency.** The shipped `sdd-deliver-and-ship` skill's
   `raw_prose` instructs writing `docs/solutions/<slug>.md`, and loading the
   shipped skill produces **no invariant-violation warning** — the ship prose and
   its gate stay self-consistent under the invariant checker. Verifiable by a
   prose-invariant test over the shipped skill. (Mechanically, the gate leaf's
   `docs/solutions/` path is auto-derived as a required prose token, so the two
   must move together — an implementation consequence the design phase records.)
4. **Promotion produces a valid pack.** Given a `docs/solutions/<slug>.md` lesson,
   the promotion flow produces a pack under `.agentalloy/custom-skills/` that
   passes `agentalloy validate-pack` in strict mode (execution + verification +
   rationale fragments, valid category, `domain_tags` within the tier's soft
   ceiling).
5. **Duplicate lessons are caught and not left in the corpus.** Promoting a
   lesson whose fragments duplicate an existing corpus skill (cosine ≥ 0.92)
   surfaces the hard-duplicate signal (`EXIT_DEDUP`, non-zero, unless
   `--allow-duplicates` is passed) *and* the promotion flow acts on it — skipping
   or rolling back the install — so the near-duplicate is not left served in the
   corpus. Verifiable by a test that promotes a near-identical lesson and asserts
   both the `EXIT_DEDUP` signal and that the skill is absent from the corpus
   afterward. (The bare `install-pack` rail does not roll back on its own; this
   AC is what makes curation real rather than advisory.)
6. **Read-path reused, not rebuilt.** No file under `src/agentalloy/code_index/`
   or `src/agentalloy/retrieval/` is modified and no new proxy/injection *surface*
   is added — the only `api/` change is D1's behavior-preserving delegation of the
   existing work-item resolver down to `contracts.py`. `docs/solutions/*.md`
   remains retrievable via `agentalloy code search` with no code change (the code
   index already ingests it). *(Refined from the original "no api/ change": design
   D1 moved the shared resolver so `signals` needn't import `api`.)*
7. **Opt-out parity.** Under `lifecycle-mode off` or `flow free`, neither the
   codify gate nor any new prompt fires — the bridge is inert, consistent with
   the existing opt-out semantics, so Options A/B remain unaffected.
8. **Override-breakage migration is documented.** A migration note exists (in the
   shipped `sdd-deliver-and-ship` skill's `change_summary` and in Piece 1 of this
   spec) stating that a pre-existing enabled profile override for
   `sdd-deliver-and-ship` that lacks the new `docs/solutions/` token is dropped at
   runtime (shipped prose served, warning logged) until its author adds the token.
   Checkable: the note is present and names the token and the drop behavior.

## Out of Scope

- Re-implementing CE's `plan`/`work`/`review` commands or shipping the CE plugin.
  Lesson prose is assumed to arrive from CE or the agent.
- **Automatic** promotion of every lesson into the corpus. Promotion is
  deliberate and dedup-gated by design — automatic promotion would reintroduce
  the bloat this feature exists to avoid.
- Any change to the **code index, retrieval engine, proxy/injection surfaces, or
  the pre-seeded corpus** (`src/agentalloy/_corpus/`). The read-path is reused.
- The full **Knowledge module** vision (a decisions graph linking rationale to
  symbols, cross-task decision history). This feature is a first concrete slice,
  not the whole module.
- **Auto-migrating** existing profile prose overrides that the new invariant
  token would invalidate. A documented migration note is in scope; rewriting
  users' overrides for them is not.
- Any cloud or paid-LLM call in the compose or gate path — enforcement stays
  deterministic, consistent with AgentAlloy's "deterministic by default" posture.
- `docs/architecture-decisions/` and `CLAUDE.md` as capture targets (CE also
  writes these). This feature standardizes on `docs/solutions/<slug>.md` as the
  single per-task lessons artifact.

## Design surface (hand-off to the design phase)

The design phase should decide the *how* for these open points; they are
recorded here so design starts grounded, not to constrain acceptance:

- **Codify-gate predicate.** Reuse `artifact_newer_than` (lessons entry fresher
  than a task-start / `docs/ship/<slug>.md` marker) vs. a new per-slug predicate
  that resolves the active slug from `.agentalloy/contracts/ship/<slug>.md`
  (mirroring `build_contracts_cover_tasks` and the #10 approval gate's
  context-derived marker path). Existence-only `artifact_exists` is ruled out by
  AC 2.
- **Which edge to gate + `--force`.** Gate the `qa → ship` edge, or ship's own
  close-out transition? And must codify survive `agentalloy phase set --force`
  (deterministic completeness leaves are bypassed by `--force` today; the
  approval gate added an unconditional `_approval_gate_blocks` carve-out to
  survive it)? *(Resolved in design: `qa → ship`, since ship's close-out is not
  gate-enforceable; no `--force` carve-out, by proportionality.)*
- **Promotion flow shape.** A new first-class CLI subcommand (e.g.
  `agentalloy codify` / `agentalloy lessons promote`) vs. an agent-driven flow
  reusing the existing `add-skill` lane and its human-approval gate. Plus the
  lesson→fragment template and how `domain_tags` are derived from the lesson.
- **Duplicate handling (per AC 5).** How the promotion flow acts on the dedup
  signal, since the `install-pack` rail does not roll back on its own: a
  *pre-ingest* similarity probe before installing, or a *post-`EXIT_DEDUP`*
  rollback of the just-installed skill. Decide which, and whether
  `--allow-duplicates` is ever surfaced to the user.
- **Lesson quality gate (optional).** Whether to additionally require an
  `artifact_contains` `sections:` set (e.g. `Problem` / `Approach` /
  `What didn't work`) on the lessons file, and the multi-file glob semantics that
  implies for `docs/solutions/*.md`.

---

*Next step per the SDD spec phase: present this spec, get explicit approval, then
`agentalloy approve spec` to seed the design work-item at
`.agentalloy/contracts/design/compound-engineering-bridge.md`. This spec is
presented and stops here — it does not advance itself.*
