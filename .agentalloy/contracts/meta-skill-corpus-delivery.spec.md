# Meta-Skill Corpus Delivery — Architecture Spec

**task_slug:** meta-skill-corpus-delivery
**route:** full
**related:** `docs/spec-contracts/install-pack-semantic-gate.md` + `.slice2.design.md`
(the concrete case that surfaced this — `sys-skill-review-verdict` referenced from
`sdd-add-skill` but unreachable); memory `meta-skill-delivery-gap` (the audit trail
and user-confirmed history: this wiring was started once and abandoned).

## Context

`src/agentalloy/_packs/meta/*.md` and `_packs/conventions/*.md` — seven skills
today, including `sys-skill-authoring-rules`, `sys-skill-transform-contract`,
`sys-r1-tiered-sourcing`, and the new `sys-skill-review-verdict` — are **not
delivered to any running agent**. This was discovered as a side effect of the
install-pack semantic gate work (slice 2's producer skill), not something this spec
sets out to fix incidentally; it is a real, independent gap with its own scope.

**What's confirmed, not guessed** (grepped and run against this repo):

- `meta/` and `conventions/` have no `pack.yaml`. `_discover_packs` only finds
  `*/pack.yaml`; neither directory appears in `agentalloy install-packs --list`.
  CI's corpus build (`corpus-nightly.yml`, `container-build.yml`) runs exactly
  `install-packs --packs all` — it cannot reach these files.
- `python -m agentalloy.bootstrap <path>` is the only code that parses
  (`skill_md/parser.py`) and inserts (`bootstrap._insert`) one of these `.md` files
  into a skill store. It is a **manual, single-file CLI** — nothing in this repo
  calls it in a loop or as part of any build/install/CI step.
- **Even if that loop existed, the skills would still not surface as currently
  authored.** `bootstrap._insert` writes `skill_class="system"`. System-class
  retrieval (`retrieval/system.py::retrieve_system_fragments`) is a pure
  deterministic predicate (`applicability.py::_is_applicable`, no embedding, no
  ranking):
  1. `always_apply=True` → always included.
  2. `phase_scope` set and the current phase is in it → check `category_scope`
     next (`None` = no restriction; set = must match `category`).
  3. `phase_scope` is `None` and `always_apply=False` → **excluded**.

  Every meta skill today has `always_apply: false` and empty `phase_scope`
  (frontmatter field present, value blank) → case 3, excluded unconditionally.
  `category_scope: tooling` is additionally moot: `orchestration/compose.py` always
  calls the predicate with `category=None`.
- **A working delivery mechanism for "a referenced-but-not-searched-for skill"
  already exists and is proven** — `requires` graph edges. `install/importer.py`
  writes `(source, target, 'requires')` edges from a YAML skill's `requires: []`
  field; `retrieval/domain.py::_graph_expand` resolves them at query time,
  appending a required skill's top fragment to the tail of the domain-retrieval
  result when the *source* skill is itself retrieved. This is how a workflow skill
  could pull in a reference skill on demand instead of via a search hit. But:
  gated behind `RETRIEVAL_GRAPH_EXPAND` (**off by default**), and `_graph_expand`
  only *promotes* a target already present in the fused embedding-search candidate
  pool (`pool_by_id`) — it is not an unconditional fetch-by-id. A rarely-matched
  reference skill might never enter that pool regardless of the edge existing.

**Why this matters concretely:** `sdd-add-skill.yaml` step 3 has told agents to
consult `sys-skill-authoring-rules` since it was written, and (per this session)
now also names `sys-skill-review-verdict`. Both references are prose-only —
neither is a `requires:` edge, and neither skill is ingested anywhere. The
instruction has always been, and remains, unfulfillable by any mechanism the
retrieval system actually runs.

## Architecture *(grounding — not binding on acceptance)*

Three fixes are viable with today's code, each reusing different existing,
working machinery. None require new retrieval infrastructure.

### Option A — Ship as a real system-governance pack, phase-scoped

Give `meta/` (or fold its skills into an existing pack, e.g. `sdd/` or a new
`meta/pack.yaml`) a manifest and per-skill YAML files, exactly like the working
`sys/` pack (which already ships 5 `skill_class: system` skills that DO surface,
because they set `always_apply`/`phase_scope` correctly). Ride the proven
`install-packs` → `ingest.py` → DB path — the same one every other pack uses. No
new code.

The applicability fix rides along naturally for skills tied to a specific SDD
phase: `sys-skill-review-verdict` is only relevant during `add-skill` — exactly the
phase `sdd-add-skill.yaml` already declares via `applies_to_phases: [add-skill]`.
Setting `phase_scope: [add-skill]`, `category_scope: null`, `always_apply: false`
satisfies `_is_applicable` rule 2 (phase matches, no category restriction) —
deterministic delivery, zero embedding involved, and it only injects during the
phase where it's relevant (unlike `always_apply: true`, which would inject into
every session). **This is the leanest fix for phase-bound meta skills** — but
doesn't fit `sys-skill-authoring-rules`/`sys-r1-tiered-sourcing` as cleanly (those
are referenced from multiple contexts, not one phase — would need a broader
`phase_scope` list or `always_apply: true`, which is a real per-skill design call,
not automatic).

### Option B — Ship as domain skills + explicit `requires` edges, enable graph-expand

Ingest meta skills as ordinary `skill_class: domain` (or `workflow`) YAML through
the same `install-packs` path, so they get embedded/fragmented like everything
else. Add `requires: [sys-skill-review-verdict]` to `sdd-add-skill.yaml` (a real
edge, not prose). Turn on `RETRIEVAL_GRAPH_EXPAND=on`. When `sdd-add-skill` is
retrieved, its required skill is promoted into the result if already in the fused
candidate pool.

Reuses the most machinery (embedding, ranking, graph-expand all already built and
tested) but has the weakest guarantee: promotion depends on the target already
scoring into the pool, and the flag is off by default repo-wide — turning it on
here is a **global** retrieval behavior change, not scoped to meta skills, with
consequences for every other pack's graph-expand-eligible `requires` edges (dormant
since the flag shipped off). Out of this spec's control once flipped.

### Option C — Finish the abandoned bootstrap-loop wiring

Automate what `bootstrap.py` does per-file into a loop over `_packs/meta/*.md` +
`_packs/conventions/*.md`, hooked into `install-packs`/corpus build (a new
subcommand or a step folded into the bulk-bootstrap path). Closest to "finish what
was started." Still requires solving the applicability-predicate exclusion
per-skill (same fix as Option A — set real `phase_scope`/`always_apply`), and
duplicates ingestion logic that already exists for the YAML path in
`ingest.py`/`install/importer.py` — a second parser/inserter to maintain
long-term. This duplication is plausibly *why* the original attempt was
abandoned; not confirmed (the branch is gone), but worth naming as a risk before
re-attempting the same shape.

### Not evaluated further here

A **skill-by-id fetch tool** exposed directly to the agent (bypassing retrieval
predicates entirely — the agent calls something like "get skill sys-skill-review-verdict"
on demand) would sidestep both the predicate-exclusion and graph-expand-pool
problems. `reads/active.py::get_active_skill_by_id` already exists as the read
function; nothing currently exposes it as an agent-facing tool/API. This is a
real fourth option but is a larger surface (new tool, new trust boundary for what
an agent can pull mid-session) — flagged for design to consider, not scoped as a
peer option here since it changes the interaction model, not just the ingestion
path.

## Acceptance Criteria

1. **Delivery is proven, not asserted.** Whichever option is chosen, a test
   demonstrates a target meta skill (e.g. `sys-skill-review-verdict`) is actually
   returned by the live retrieval/compose path under the conditions design defines
   (a specific phase, or a specific query) — not merely "present in the DB."
2. **No regression to the 5 working meta skills' current (non-)behavior**, unless
   design explicitly chooses to also fix their applicability (recommended, but a
   separate call per skill — `sys-skill-authoring-rules` is referenced from more
   than one phase).
3. **`sdd-add-skill.yaml`'s references become real, resolvable pointers** — either
   a `requires:` edge (Option B) or documented as reachable via phase-scoped
   system delivery (Option A) — not prose the retrieval system cannot act on.
4. **No global behavior change as an unreviewed side effect.** If design selects
   Option B, flipping `RETRIEVAL_GRAPH_EXPAND` to `on` repo-wide is called out and
   confirmed explicitly — it affects every pack's `requires` edges, not just
   meta's.
5. **Whichever mechanism is chosen is documented as the meta-skill delivery
   contract**, so the next skill author knows how to make a new meta skill
   reachable instead of re-discovering this gap.

## Out of Scope

- **Flipping `AGENTALLOY_INSTALL_REQUIRE_REVIEW`** — unrelated prior feature
  (install-pack-semantic-gate); this spec does not touch it.
- **A general agent-facing skill-by-id fetch tool** — named above as a real
  alternative, deliberately not scoped here; would need its own spec if pursued.
- **Auditing every existing pack's `requires` edges for graph-expand readiness** —
  if Option B is chosen, that's a global retrieval-behavior review beyond this
  spec's meta-skill-specific scope.
- **Re-investigating the abandoned prior attempt** — the branch/worktree is
  confirmed gone (user, this session); not recoverable, not a research task here.

## Design surface (hand-off to the design phase)

- **Which option (A/B/C), or a mix** — e.g. Option A for phase-bound skills
  (`sys-skill-review-verdict`) and a separate call for multi-context reference
  skills (`sys-skill-authoring-rules`, `sys-r1-tiered-sourcing`) that don't fit one
  `phase_scope`. Not resolved here; recommend Option A for the motivating case
  (leanest, reuses proven machinery, no global flag flip) as a starting lean, not
  a decision.
- **Per-skill applicability metadata** — if Option A, what `phase_scope`/
  `always_apply` each of the 7 current meta skills should carry (a real per-skill
  editorial call, not mechanical).
- **Pack placement** — new `meta/pack.yaml`, or fold into `sdd/` (since most
  current consumers are SDD workflow skills), or per-consumer packs.
- **Whether to also revisit `sys-skill-review-verdict`'s design** (from
  `install-pack-semantic-gate.slice2.design/`) once it has a real delivery path —
  it was authored assuming reference-doc-style access; a phase-scoped
  auto-injected version might warrant re-reading for length/tone (system fragments
  inject in full, unlike domain fragments which are ranked/truncated).
