# Spec: Make the SDD fast lane real — route-field-authoritative intake + `sdd-fast → qa → ship`

## Problem

The SDD workflow advertises a fast lane, but two things are broken:

1. **`route: fast` is silently ignored at intake.** An intake contract can declare
   `route: fast`, but the transition engine always advances `intake → spec`
   (the full lane) regardless of the field. Observed in the v3.2.3 SDD e2e.
2. **The fast lane skips QA.** Today `sdd-fast → ship` (`gates.py:36`) — the fast
   lane bypasses verification entirely. It is also *only* a compressed
   build (its prose is "spec → build → test → deliver", no design step, and it
   gates only on `src/**` + `tests/**`).

### Target model

`intake` decides the lane. `sdd-fast` is a **compressed `spec + design + build`
for a single task (no build loop)** that then **merges into the standard
`qa → ship`** — identical verification and delivery to the full lane:

```
intake ──► spec ──► design ──► build ──► qa ──► ship      (full)
   └─────► sdd-fast ─────────────────────► qa ──► ship    (fast)
```

QA is deliberately **not** compressed into fast: spec/design/build scale down with
task size, but verification doesn't — a small change still needs real checks, and
the QA/ship gates are already cheap. Fast and full converge on one QA definition
and one ship definition, maintained once.

### Root cause of #1: two routing signals that don't agree

1. **Contract field** — `Contract.route: str = "full"` (`src/agentalloy/contracts.py:64-66`),
   parsed + validated (`full|fast`, `contracts.py:189-203`). **No transition code
   ever reads it.** (Only readers: `tests/test_contracts.py::TestContractRoute`.)
2. **Directory presence** — the wired signal: `_intake_route_hint(project_root)`
   (`src/agentalloy/signals/skill_loader.py:341-356`) returns `"sdd-fast"` *iff*
   `.agentalloy/contracts/sdd-fast/*.md` exists, else `None`. Passed as
   `next_phase_hint` to `decide_transition()` (`proxy_signal.py:293`,
   `gates.py:262,273`: `to_phase = next_phase_hint or _PHASE_GRAPH.get(current_phase)`).

When the agent declares `route: fast` but writes the next work-item into
`contracts/spec/` (the e2e behavior), the two signals disagree and the field
loses → full lane.

## Design

### A. Route is field-authoritative (the gate stays route-agnostic)

Mirror the full lane's existing pattern: intake's exit gate is intentionally
route-agnostic — `artifact_exists: .agentalloy/contracts/**/*.md`
(`sdd-intake.yaml:15-17`) — so it's satisfied by a next-phase work-item in *any*
folder. Routing is then a soft hint layered on top. We make that hint read the
**field**, and trust it (the full lane likewise never hard-gates *which* folder a
contract lands in — see "Decision: trust the field" below).

Rewrite `_intake_route_hint()`:

- New `_read_intake_route(project_root) -> str | None`: glob
  `.agentalloy/contracts/intake/*.md`, parse the newest by mtime via
  `parse_contract()`, return `.route` (`"full"|"fast"`). Any failure (no dir, no
  file, malformed, unreadable) → `None`. Best-effort; never raises.
- `_intake_route_hint()`:
  - `route = _read_intake_route(project_root)`
  - `route == "fast"` → return `"sdd-fast"`.
  - `route == "full"` → return `None` (→ `spec`).
  - `route is None` → existing directory-presence fallback (preserves BC + the
    no-contract path).

**Decision: trust the field (option A from discussion).** When `route == "fast"`,
return `"sdd-fast"` unconditionally — do *not* add a content guard requiring a
`contracts/sdd-fast/` work-item. Rationale: intake's gate (`contracts/**/*.md`) is
already met, so `should_transition == True` and the missing-artifact advisory
machinery in `decide_transition()` (`gates.py:281`, only fires `if not
should_transition`) **cannot** fire — a content guard would need bespoke advisory
plumbing for no real benefit. This matches how the full lane treats a misfiled
contract: it trusts and advances; the destination phase composes against whatever
work-item exists (and prompts if thin). The inverse disagreement is still fixed:
`route: full` + a stray `sdd-fast/` file → field wins → `spec`.

### B. Graph: `sdd-fast → qa`

`src/agentalloy/signals/gates.py:36`: change `"sdd-fast": "ship"` →
`"sdd-fast": "qa"`. Update the adjacent comment. No other topology changes; the
`qa → ship → ship` tail is reused verbatim.

### C. `sdd-fast.yaml` — compressed spec+design+build, single task, hand to QA

Rewrite `src/agentalloy/_packs/sdd/sdd-fast.yaml`:

**Exit gate** — mirror the full lane (gated doc with required sections + code/tests),
collapsing spec's 2 docs + design's 3 docs into one combined doc:

```yaml
exit_gates:
  all_of:
    - artifact_exists:
        path: docs/fast/*.md
    - artifact_contains:
        path: docs/fast/*.md
        sections:
          - Acceptance Criteria   # ← from full spec (docs/spec)
          - Approach              # ← from full design (approach.md)
          - Test Cases            # ← from full design (test-plan.md)
    - artifact_exists:
        path: "src/**"
    - artifact_exists:
        path: "tests/**/*.py"
```

Section names deliberately reuse the full lane's exact headings so the compressed
doc reads like a mini full-lane. `Tasks` is intentionally **omitted** — the fast
lane is a single task with no build loop, so a task list is redundant by
construction. (Note: this fixes a latent bug in the current gate — line 19-21 uses
`artifact_contains` with `sections: []` on `tests/**/*.py`, which is just an
existence check mislabeled; replaced with `artifact_exists`.)

**Prose** (`raw_prose`) — rewrite the "one pass" as compressed
**spec → design → build → test**, then **hand to QA** (not ship):

- Spec-in-a-brief: state Acceptance Criteria crisply (bail to `spec` if you can't).
- Design-in-a-brief: the Approach — the single shape of the change; if it needs
  real multi-step design, bail.
- Build: make the one change inside `scope.touches`; no build loop — if it
  fragments into multiple tasks, bail.
- Test: write the Test Cases that prove acceptance, run green.
- Capture all three (Acceptance Criteria / Approach / Test Cases) in
  `docs/fast/<slug>.md`, then **`agentalloy phase set qa`** — *not* ship. The
  guard refuses the jump until `docs/fast/*.md` (with those sections) + `src/` +
  `tests/` exist.
- Keep the **bail-to-`spec`** escape hatch and the "not this / don't fake-fast it"
  guidance.

Update `description` and `change_summary` to say the lane merges into standard
`qa → ship` (drop "deliver" language that implied a direct hand to ship).

### D. Pack version bump (mandatory for propagation)

`src/agentalloy/_packs/sdd/pack.yaml:2`: bump `version: 1.0.15` → `1.0.16`. Pack
edits propagate to embedded skills only on a version bump (project memory
`pack-versioning-by-design`); without it the rewritten `sdd-fast` prose/gate won't
re-embed.

### E. Doc/comment reconciliation

- `contracts.py:64-66` (`Contract.route` comment): update to state the field is the
  authoritative routing decision read by `_intake_route_hint`.
- `_intake_route_hint` docstring (`skill_loader.py:341-349`): describe
  field-authoritative behavior with directory-presence fallback.

## Files touched

| File | Change |
|---|---|
| `src/agentalloy/signals/skill_loader.py` | `_read_intake_route()` helper; rewrite `_intake_route_hint()` + docstring |
| `src/agentalloy/signals/gates.py` | `_PHASE_GRAPH["sdd-fast"] = "qa"` + comment |
| `src/agentalloy/_packs/sdd/sdd-fast.yaml` | exit gate (combined doc + code/tests), prose (spec+design+build→qa), description/change_summary |
| `src/agentalloy/_packs/sdd/pack.yaml` | `version` 1.0.15 → 1.0.16 |
| `src/agentalloy/contracts.py` | `Contract.route` comment reconcile |
| `tests/test_contracts.py` | route-field-driven hint tests (below) |
| `tests/` (gates/transition) | `sdd-fast → qa` transition test |

## Tests

`tests/test_contracts.py` — extend `TestIntakeRouteHint` (and fix its docstring,
which asserts the old directory-only design):

1. intake contract `route: fast` → `_intake_route_hint` returns `"sdd-fast"`
   (no `sdd-fast/` folder needed — field is authoritative).
2. intake contract `route: full` + a stray `contracts/sdd-fast/*.md` present →
   returns `None` (field wins; full lane). *New inverse-disagreement guarantee.*
3. No intake contract, `sdd-fast/` present → `"sdd-fast"` (directory fallback
   preserved; existing `test_fast_contract_hints_sdd_fast` still passes).
4. Malformed intake contract → falls back to directory presence; never raises.

Transition test (gates):

5. `decide_transition(current_phase="sdd-fast", gate_spec=<sdd-fast gates>, …)`
   with `docs/fast/*.md` (all three sections) + `src/` + `tests/` present →
   `to_phase == "qa"`, `should_transition == True`.
6. Same, missing `Approach` section → does not transition; advisory names the
   `docs/fast/*.md` deliverable.
7. `_PHASE_GRAPH["sdd-fast"] == "qa"` (guards the topology).

Pack-consistency test (if one exists for exit_gates parity) updated for the new
`sdd-fast` gate shape.

## Verification

- `uv run pytest tests/test_contracts.py tests/ -q -m "not integration and not container"`.
- `ruff format --check` + `ruff check` + `pyright` (CI parity — memory
  `ci-ruff-format-check`).
- Live SDD e2e fast lane after a rebuild: an intake contract with `route: fast`
  advances `intake → sdd-fast`; a `docs/fast/<slug>.md` (Acceptance Criteria /
  Approach / Test Cases) + `src/` + `tests/` advances `sdd-fast → qa`; standard
  QA + ship gates carry to completion. `route: full` still runs the full chain.

## Out of scope

- Mid-stream lane switching beyond the existing bail-to-`spec` escape hatch.
- Any change to spec/design/build/qa/ship gate definitions (fast reuses qa/ship
  verbatim).
- Reranker/embed/gate-predicate engine changes — gates remain structural.
