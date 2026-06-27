# Plan #12 — Build-contract density prose + design→build coverage gate (§G)

**Source root:** `/home/nmeyers/dev/claude/agentalloy/.claude/worktrees/feedback` (v3.11.1)
**PLAN-OF-ATTACK section:** §G (finding #5). Sequencing slot #7.
**Batch:** CORPUS (raw_prose + contract_template are embedded/composed → re-embed +
`pack.yaml` version bump + image rebuild). The predicate/gate **code** is DB-free and
enforces on package reinstall, but rides the same image rebuild as the prose, so the
whole item lands in the corpus batch.
**Effort:** M · **Risk:** low.

---

## Locked decisions (per PLAN-OF-ATTACK §9 — these override any divergent value below)

- **D3 / `{K}` = 4** — every `{K}` reference resolves to #13's locked `DEFAULT_K_BY_PHASE["build"] = 4`. ✓ matches this plan. If the post-#15 sweep (decision gate D3) moves the default, the prose `{K}` and the build-contract template move with it.
- **One SDD `pack.yaml` bump (1.0.19→1.0.20)** covers this plan's density prose **and** #10's present-and-STOP rider; a single editor owns `sdd-design-and-planning.yaml` (§6 prose + `exit_gates.all_of`), appending **both** predicates (`build_contracts_cover_tasks` + `approval_recorded`).

---

## 0. Problem restated (from §G)

Design handed build a single monolithic 7-tag contract spanning all 8 tasks. At the
build-phase retrieval cap (`DEFAULT_K_BY_PHASE["build"]`, currently 2; raised by #13),
5 of 7 tech surfaces got **zero** fragments. Root cause is workflow prose, not engine:

- §6 ("Emit one build contract per task") is **soft framing**, never a hard MUST, never
  names the k cap.
- §3 vertical-slicing prose + §6's 4-tag example (`pytest`, `fastapi`, `duckdb`, `async`)
  actively **model** the multi-tag dilution that starves small-k retrieval.
- The design exit-gate only checks `artifact_exists: .agentalloy/contracts/build/*.md`
  (≥1 contract), so a single all-feature monolith passes the gate clean.

Fix = prose granularity + a hard MUST + a **counting** exit-gate (contracts ≥ tasks).
Decomposition alone gave ~4x coverage with the reranker disabled — pure prose/template
fix plus one new deterministic predicate. Orthogonal to Stage B (these composes ran
`lm_outcome=disabled`).

---

## 1. Decisions required before coding

1. **k-cap number in the prose (coordinate with #13).** #13 raises
   `DEFAULT_K_BY_PHASE["build"]` from 2. The §6 MUST and the gate advisory cite this
   number. **Recommend writing `~4`** (matches §E "2→4") with the phrasing "the build
   phase's k-skill budget (`DEFAULT_K_BY_PHASE["build"]`)" so the prose stays correct if
   #13 lands a different value. **Do not hardcode `2`.** Wherever this plan shows `{K}`,
   substitute #13's final value (default 4).
2. **Task-counting convention.** The gate counts **top-level markdown list items**
   (bullet `-`/`*`/`+` or ordered `1.`/`1)`, ≤3 leading spaces) under any `## Tasks`
   heading in `tasks.md`. This convention must be stated in §6 prose so authors know
   what is counted. Floor clamps to 1 (never relaxes today's ≥1 gate; never blocks on an
   unparseable tasks.md). **Confirm this list-item convention is acceptable** (alternative
   ">= distinct tech surfaces" was rejected as non-deterministic to parse).
3. **Coordinate the shared `sdd-design-and-planning.yaml` + `predicates.py` + `gates.py`
   edits with the approval-gate todo (§H).** §H *also* adds a predicate to `predicates.py`
   `PREDICATES`, adds prose to `sdd-design-and-planning.yaml`, and wires a node into the
   design `all_of` exit-gate. These are the same three files. Land both in one branch or
   sequence them; do not let two branches rewrite the design `all_of` block independently.

---

## 2. Dependencies / ordering

- **Depends on #13's k decision** (for the prose number only; authoring can proceed with
  `{K}=4` and be reconciled).
- **Re-embed is shared** with §E/§F/§8 (#13/corpus-authoring/fragment-atomicity) and §H.
  Batch ONE corpus rebuild + image rebuild. Bump `_packs/sdd/pack.yaml version` once for
  all sdd-pack edits in the batch.
- The fragment-atomicity reslice (§8) and the K sweep (#13) are sequenced *before* the K
  test, but **do not block this prose work** — these composes are `lm_outcome=disabled`
  and the gate is corpus-independent.

---

## 3. Files touched (for conflict detection)

| File | Change | Lane |
|------|--------|------|
| `src/agentalloy/_packs/sdd/sdd-design-and-planning.yaml` | §3 + §6 + "Not this" prose; add gate node to `exit_gates.all_of` | CORPUS (prose) + CODE (gate node) |
| `src/agentalloy/_packs/sdd/sdd-build.yaml` | `contract_template`: annotate `domain_tags`, tighten `## Task` placeholder | CORPUS |
| `src/agentalloy/_packs/sdd/pack.yaml` | `version: 1.0.19` → `1.0.20` (bump once for batch) | CORPUS |
| `src/agentalloy/signals/predicates.py` | new `_count_task_items` + `eval_build_contracts_cover_tasks` + registry entry | CODE |
| `src/agentalloy/signals/gates.py` | advisory hook for the new predicate | CODE |
| `tests/test_predicates.py` | predicate unit tests | CODE (test) |
| `tests/test_gates.py` | design-gate block/pass tests | CODE (test) |
| `tests/test_sdd_build_density.py` (NEW) | prose-drift golden + exit_gates structural | CODE (test) |

**Shares files with:** the **approval-gate todo (§H)** —
`sdd-design-and-planning.yaml`, `predicates.py` (`PREDICATES` registry), `gates.py`,
`pack.yaml`. **Coordinate-only with #13** (k value; no file overlap — #13 edits
`compose_models.py`/`proxy_apply.py`/`domain.py`, none of which this item touches).

---

## 4. EDIT 1 — `sdd-design-and-planning.yaml` §3 (prose, CORPUS)

§3 currently (lines 112-121):

```
  ## 3. Slice it vertically, in dependency order

  Decompose into thin slices that each deliver a working, testable path — schema
  + API + UI for one capability — not horizontal layers (all schema, then all
  API). Order so foundations come first and every slice leaves the system green.
  Each task names the spec acceptance it satisfies; don't invent new acceptance
  here — that's spec's job.

  Keep tasks small: one focused session each. If you're writing "and" in a task
  title, it's two tasks.
```

**Add one paragraph after the "Keep tasks small" paragraph** (after line 121), stating
build contracts are finer than design slices and center ONE dominant surface:

```
  A build contract is *finer* than a design slice. A slice may span schema + API +
  UI; a build contract centers **one dominant technology surface** so the proxy can
  front-load the handful of skills that match it. In §6 you split each slice into
  one contract per task — so slice with that in mind: each task should resolve to a
  single tech surface, not a basket of them.
```

Rationale: §3 currently *models* the multi-surface slice without warning that build
contracts must narrow further. This paragraph sets up the §6 MUST.

---

## 5. EDIT 2 — `sdd-design-and-planning.yaml` §6 (prose, CORPUS) — the core change

### 5a. Promote to a hard MUST + state the k cap

§6 currently opens (lines 147-151):

```
  ## 6. Emit one build contract per task

  This is the hand-off that makes build effortless. For each task in `tasks.md`,
  write a build contract — design is the cheap place to do this, with the whole
  plan in front of you:
```

**Replace the opening paragraph** with a hard MUST that names the k budget:

```
  ## 6. Emit one build contract per task

  **MUST: emit ONE build contract per task — never one whole-feature contract.**
  Build retrieval is budgeted to a small ~{K}-skill cap per contract (the `build`
  default in `DEFAULT_K_BY_PHASE`). A single contract carrying every task's tags
  spreads that budget across all of them, so most surfaces get **zero** skills — a
  7-tag contract at k≈{K} front-loads two surfaces and starves five. One contract
  per task, each centered on one surface, is what keeps the budget on target. This
  is the cheap place to do it, with the whole plan in front of you:
```

> `{K}` = #13's final `DEFAULT_K_BY_PHASE["build"]` (recommend **4**).

### 5b. Replace the 4-tag example with one-dominant-plus-at-most-one-adjacent

Current `domain_tags` instruction (lines 155-157):

```
  In each, fill `domain_tags` with that task's *technology surface* — the
  concrete tech the task touches (`pytest`, `fastapi`, `duckdb`, `async`), **not**
  process words (`build`, `coding`) — and write the `## Task` body as a
```

**Replace the parenthetical example** so it models a single dominant surface, not a
union:

```
  In each, fill `domain_tags` with that task's *technology surface* — **one
  dominant surface plus at most one adjacent** (`[react]`, `[fastapi, async]`,
  `[typescript, pure-functions]`), **not** the union of every surface in the
  feature and **not** process words (`build`, `coding`) — and write the `## Task`
  body as a
```

### 5c. State the task-counting convention (so the gate is predictable)

In the "verify the hand-off is complete" paragraph (lines 164-174), the prose already
says "every task in `tasks.md` has a matching contract". **Append one sentence** naming
what "a task" is for the gate (insert after line 165 "…in `.agentalloy/contracts/build/`. The"):

```
  Each task is a top-level list item under `## Tasks`; the design→build gate counts
  those items and refuses the jump until at least that many build contracts exist
  under `.agentalloy/contracts/build/`.
```

Also update the trailing guard sentence (lines 170-174) which currently says the guard
needs "**at least one** build contract" — change "at least one build contract exists" to
"**one build contract per task** exists" so the prose matches the new gate.

### 5d. Anti-pattern in "## Not this"

"## Not this" currently ends at line 184. **Add a new bullet/sentence** forbidding the
all-tech contract:

```
  And don't collapse the worklist into one all-tech contract — a single
  `[react, typescript, fastapi, duckdb, …]` body for the whole feature is the
  anti-pattern this phase exists to prevent: at k≈{K} it front-loads two surfaces
  and starves the rest. One single-surface contract per task, always.
```

---

## 6. EDIT 3 — `sdd-design-and-planning.yaml` exit_gates (gate node, CODE-immediate)

Current tail of `exit_gates.all_of` (lines 42-46):

```yaml
    # §6 hand-off: design must emit at least one build contract before build can
    # walk them with `task next`. Without this floor, `phase set build` passes with
    # zero contracts and build has no worklist (the gap that motivated this gate).
    - artifact_exists:
        path: .agentalloy/contracts/build/*.md
```

**Append a new leaf node after it** (keep the existing `artifact_exists` for its
missing-path advisory; the count node adds the density floor):

```yaml
    # §6 density floor: one build contract PER TASK, not one whole-feature contract.
    # Counts top-level list items under `## Tasks` in tasks.md and requires at least
    # that many build contracts. Floor-clamps to 1 so it never relaxes the
    # artifact_exists check above and never blocks on an unparseable tasks.md.
    - build_contracts_cover_tasks:
        tasks: docs/design/**/tasks.md
        contracts: .agentalloy/contracts/build/*.md
```

`exit_gates_for_phase` (`signals/skill_loader.py:469`) reads this packaged YAML directly
(DB-free), so `agentalloy phase set build` and the proxy auto-transition enforce it as
soon as the package/image carries the new YAML **and** the new predicate (Edit 4). No DB
re-embed needed for the gate itself — but ships in the same image rebuild as the prose.

**Ordering note:** `all_of` short-circuits on the first `NOT_MET` (`gates.py:131-137`).
The `artifact_exists`/`artifact_contains` checks for `tasks.md` precede this node, so the
count predicate only evaluates once `tasks.md` exists — if it's missing the earlier nodes
block first with a clean advisory.

---

## 7. EDIT 4 — `signals/predicates.py` (new predicate, CODE)

`re` is already imported (line 9). Add two functions before the `PREDICATES` dict
(line 404) and register the predicate.

```python
def _count_task_items(content: str) -> int:
    """Count top-level task entries under any '## Tasks' heading.

    A task entry is a top-level (<=3 leading spaces) markdown list item — bullet
    (-, *, +) or ordered (1. / 1)) — appearing after a '## Tasks' heading and
    before the next '## ' heading. Nested/indented items and prose lines are
    ignored. Heading match reuses the trailing-qualifier tolerance of
    _section_present (so '## Tasks (8)' counts).
    """
    item_re = re.compile(r"^\s{0,3}(?:[-*+]|\d+[.)])\s+\S")
    count = 0
    in_tasks = False
    for line in content.splitlines():
        if line.lstrip().startswith("## "):
            heading = line.lstrip("#").strip()
            in_tasks = _section_present("Tasks", [heading])
            continue
        if in_tasks and item_re.match(line):
            count += 1
    return count


def eval_build_contracts_cover_tasks(
    args: dict[str, Any], ctx: PredicateContext
) -> PredicateResult:
    """MET iff #build-contracts >= #tasks enumerated in tasks.md (floor 1).

    Deterministic, embed-free. Counts top-level list items under every '## Tasks'
    heading across the tasks glob, clamps the task floor to 1 (so this never
    relaxes the existing >=1-contract gate and never blocks on an unparseable
    tasks.md), and compares against the count of build-contract files.
    UNKNOWN when no tasks.md exists or one is unreadable (a preceding
    artifact_exists/contains node handles the missing-file case in the all_of).
    """
    tasks_glob = args.get("tasks", "docs/design/**/tasks.md")
    contracts_glob = args.get("contracts", ".agentalloy/contracts/build/*.md")
    task_files = _glob_files(ctx.project_root, tasks_glob)
    if not task_files:
        return PredicateResult.UNKNOWN
    task_count = 0
    for f in task_files:
        content = _read_file(f)
        if content is None:
            return PredicateResult.UNKNOWN
        task_count += _count_task_items(content)
    task_count = max(1, task_count)
    contract_count = len(
        [p for p in _glob_files(ctx.project_root, contracts_glob) if p.is_file()]
    )
    return (
        PredicateResult.MET
        if contract_count >= task_count
        else PredicateResult.NOT_MET
    )
```

Registry (line 404-418) — add:

```python
    "build_contracts_cover_tasks": eval_build_contracts_cover_tasks,
```

`evaluate_predicate` (line 421) wraps in a `try/except → UNKNOWN`, so the predicate is
fail-open by construction.

---

## 8. EDIT 5 — `signals/gates.py` (advisory hook, CODE)

Without an advisory, a count-block produces no helpful message: `decide_transition`'s
missing-path advisory (lines 282-311) only fires for paths that don't glob, and
`.agentalloy/contracts/build/*.md` *does* glob (≥1 file). Mirror the
`artifact_completeness` advisory pattern.

1. Extend the predicates import (lines 13-20) to add `_count_task_items`:

```python
from agentalloy.signals.predicates import (
    PREDICATES,
    PredicateContext,
    PredicateResult,
    _count_task_items,  # pyright: ignore[reportPrivateUsage]
    _glob_files,  # pyright: ignore[reportPrivateUsage]
    _read_file,  # pyright: ignore[reportPrivateUsage]
    evaluate_predicate,
)
```

2. Add an advisory builder near `_build_completeness_advisory` (line 61):

```python
def _build_contract_coverage_advisory(
    args: dict[str, Any], ctx: PredicateContext
) -> str | None:
    """Advisory for a build_contracts_cover_tasks NOT_MET (the density block)."""
    tasks_glob: str = args.get("tasks", "docs/design/**/tasks.md")
    contracts_glob: str = args.get("contracts", ".agentalloy/contracts/build/*.md")
    try:
        tasks = 0
        for f in _glob_files(ctx.project_root, tasks_glob):
            tasks += _count_task_items(_read_file(f) or "")
        tasks = max(1, tasks)
        contracts = len(
            [p for p in _glob_files(ctx.project_root, contracts_glob) if p.is_file()]
        )
    except Exception:
        return None
    return (
        f"Design emitted {contracts} build contract(s) for {tasks} task(s) in "
        f"tasks.md. Emit ONE build contract per task before advancing to build — "
        f"`agentalloy contract init --phase build --slug <NN-task-slug>` for each, "
        f"each centered on a single tech surface."
    )
```

3. In `evaluate_node`, attach it on NOT_MET. Today (lines 175-189):

```python
    advisory: str | None = None
    if predicate_name == "artifact_completeness":
        advisory = _build_completeness_advisory(args, ctx)

    try:
        result = _evaluate_single(predicate_name, args, ctx, lm_client, qwen_calls)
    except ValueError:
        result = PredicateResult.UNKNOWN
    eval_record = GateEvaluation(...)
```

Insert after the `try/except` computes `result` (the two predicate names are mutually
exclusive, so reusing `advisory` is safe):

```python
    if (
        predicate_name == "build_contracts_cover_tasks"
        and result == PredicateResult.NOT_MET
    ):
        advisory = _build_contract_coverage_advisory(args, ctx)
```

The advisory flows through `all_evals → e.advisory → PhaseTransitionDecision.advisories`
(lines 269-271, 320) and is surfaced to the agent on the blocked transition.

---

## 9. EDIT 6 — `sdd-build.yaml` `contract_template` (CORPUS)

`contract_template` (lines 32-61). Two edits inside the `|` block.

### 9a. Annotate `domain_tags` (line 37)

```yaml
  domain_tags: []
```
→
```yaml
  domain_tags: []  # ONE dominant tech surface + at most one adjacent — never every surface
```
(A `#` comment in front-matter YAML is valid and is what the rendered contract carries.)

### 9b. Tighten the `## Task` placeholder (lines 50-53)

Current:

```
  <one self-contained build task — what to implement and the boundary of "this".
  This body IS the retrieval prompt: the proxy composes the skills that match it
  and front-loads them before you code, so describe the work concretely (the
  component, the behavior, the tech surface), not just a pointer to tasks.md.>
```

→

```
  <one self-contained build task on ONE dominant tech surface — what to implement
  and the boundary of "this". This body IS the retrieval prompt: the proxy composes
  the ~k skills that match it, so a task spanning many surfaces starves most of
  them. Describe one surface concretely (the component, the behavior, the tech),
  not a whole feature and not just a pointer to tasks.md.>
```

---

## 10. EDIT 7 — `pack.yaml` version bump (CORPUS)

`src/agentalloy/_packs/sdd/pack.yaml:2` `version: 1.0.19` → `1.0.20`. **Bump once** for
the whole sdd-pack batch (this item + §H if it lands together). Pack edits propagate only
on a version bump (memory: pack-versioning-by-design).

---

## 11. Tests

### `tests/test_predicates.py` (extend)

Use a `tmp_path` project root with `docs/design/<slug>/tasks.md` and
`.agentalloy/contracts/build/NN-*.md` files; build a `PredicateContext(project_root=...,
contracts_root=...)`.

- `test_cover_tasks_met` — tasks.md with 3 top-level list items, 3 build contracts → MET.
- `test_cover_tasks_not_met_monolith` — 8 list items, 1 contract → NOT_MET (the bug case).
- `test_cover_tasks_no_tasks_file` — no tasks.md → UNKNOWN.
- `test_cover_tasks_unparseable_clamps_to_one` — `## Tasks` heading with prose only (0
  items): 1 contract → MET; 0 contracts → NOT_MET (floor=1 == today's behavior).
- `test_count_task_items_mixed` — ordered (`1.`) + bullet (`-`) counted; indented
  sub-bullets (`    - `) and lines outside `## Tasks` ignored; `## Tasks (8)` heading
  recognized.

### `tests/test_gates.py` (extend)

- `test_design_gate_blocks_when_contracts_fewer_than_tasks` — full design folder
  (approach/tasks/test-plan with required sections) + tasks.md listing N>1 tasks + 1
  build contract → `decide_transition("design", exit_gates_for_phase("design"), ctx)`
  has `should_transition is False` and an advisory containing "build contract".
- `test_design_gate_passes_when_contracts_cover_tasks` — same, N contracts for N tasks →
  `should_transition is True`.
- `test_design_gate_floor_one_contract` — list-less tasks.md + 1 contract → passes (guard
  against false-block regression).

### `tests/test_sdd_build_density.py` (NEW — prose-drift + structural golden)

Load YAML via `yaml.safe_load`; mirror the `_PACKS` path style of
`tests/test_sdd_prose_handoffs.py`.

- `test_section6_is_hard_must` — design `raw_prose` contains `"MUST"` and
  `"ONE build contract per task"` (case-tolerant) and `"never one whole-feature"`.
- `test_section6_names_k_cap` — design prose mentions `DEFAULT_K_BY_PHASE` and a small-k
  starvation statement.
- `test_four_tag_example_removed` — design prose does **not** contain the literal
  `` `pytest`, `fastapi`, `duckdb`, `async` `` tuple; **does** contain
  `"one dominant"` and `"at most one adjacent"`.
- `test_all_tech_anti_pattern_present` — "## Not this" / prose forbids the all-tech
  contract (assert `"all-tech contract"` and a `[react, typescript, fastapi` fragment).
- `test_build_template_single_surface` — `sdd-build.yaml` `contract_template` `## Task`
  placeholder contains `"ONE dominant tech surface"` and the `domain_tags` annotation
  contains `"never every surface"`.
- `test_design_exit_gate_has_coverage_node` — `exit_gates.all_of` of
  `sdd-design-and-planning.yaml` contains a node keyed `build_contracts_cover_tasks`
  (structural; guards against the gate being dropped on a prose rewrite).

### Dogfood (manual, post-rebuild)

Run an SDD design phase with N>1 tasks; assert `phase set build` is **refused** with a 1
contract and **passes** with N. Confirm the advisory text surfaces.

---

## 12. Deploy / re-embed checklist (CORPUS batch)

1. Land Edits 1-7 + tests; `pytest tests/test_predicates.py tests/test_gates.py
   tests/test_sdd_build_density.py` green (code lane — runs without re-embed).
2. Batch with §E/§F/§8/§H sdd-pack edits → **one** `pack.yaml` version bump (1.0.20).
3. Re-embed the corpus (locks DuckDB held by the running service — coordinate a service
   restart; memory: re-embed-locks-corpus).
4. Rebuild the container image (the live proxy is the image, not the source tree) so both
   the new packaged YAML (gate + prose) and the new predicate/gate code ship.
5. Config-consistency: the new `build_contracts_cover_tasks` predicate is referenced in
   the packaged design gate — `test_design_exit_gate_has_coverage_node` is the drift
   guard. Ensure §H's `approval_recorded` node and this node coexist in the same
   `all_of` if both land.

---

## 13. Risk notes

- **Low risk.** Prose changes are inert until re-embed; the gate predicate is fail-open
  (try/except → UNKNOWN) and floor-clamped (never blocks below today's ≥1 behavior).
- **Only real regression vector:** a legitimately list-less tasks.md format. Mitigated by
  the floor=1 clamp + `test_design_gate_floor_one_contract`. If teams use a non-list
  tasks.md, the gate degrades to "≥1 contract" (status quo), not a hard block.
- **Coordination risk with §H** on the design `all_of` block and `PREDICATES` registry —
  see Decision 3. Resolve by landing both in one branch or strict sequencing.
