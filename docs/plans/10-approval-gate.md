# Plan #10 — Human-in-the-loop approval gate (§H)

**Batch:** CODE (enforcement is DB-free / runs from the installed wheel + packaged-YAML reads).
The only re-embed-gated piece is the `raw_prose` present-and-STOP rewrite in two SDD packs,
which rides the §E/§F/§G/§8 corpus rebuild that is already happening today. The gate *enforces*
without any embed.

**Independence:** PLAN §3 row 8 — depends on nothing. Touches none of the Stage B / retrieval /
telemetry code. The one cross-item file overlap is `sdd-design-and-planning.yaml` (also edited by
§G build-contract density) and `sdd-spec-and-scoping.yaml`.

## Locked decisions (per PLAN-OF-ATTACK §9 — these override any divergent value below)

- **D7 / threat model = cooperative-trust + `artifact_sha256` + telemetry-detectability + `--force` carve-out. LOCKED.** NOT hard unforgeability. (The §3 phrase "unforgeable-by-force" means `--force` must not *silently* bypass approval — consistent with D7; the marker itself is cooperative-trust.) ✓ matches this plan.
- **D8 / `SDD_FAST_REQUIRE_APPROVAL` = OFF by default. LOCKED.** Gate ON only for spec→design and design→build on the **full** lane; sdd-fast stays ungated unless the flag is set. ✓ matches this plan.
- **One SDD `pack.yaml` bump (1.0.19→1.0.20)** covers this plan's present-and-STOP prose rider **and** #12's density prose; a single editor owns `sdd-design-and-planning.yaml` (§6 prose + `exit_gates.all_of`).

**Owner decision required before coding:** none blocking. Two policy knobs are pinned below
(approval `since` globs; `>=` mtime comparison). One coordination item: fold the two `raw_prose`
sentence rewrites into the shared corpus re-embed.

---

## 0. Mechanism recap (why this is mostly isolated)

- **Exit gates are read DB-free.** `skill_loader.exit_gates_for_phase()` (skill_loader.py:469)
  loads the packaged `_packs/sdd/*.yaml` directly, not the DuckDB corpus. So adding an
  `approval_recorded` leaf to a pack's `exit_gates.all_of` changes gate behavior the moment the
  wheel/image carries the new YAML — **no embed, no SkillVersion bump** for the *gate*.
- **Two forward-mutation sites** both consume that gate spec:
  1. **Proxy auto-transition** — `proxy_signal.py:531-540`: `decide_transition(... gate_spec=exit_gates ...)`
     then `_write_phase_atomic(cwd, decision.to_phase)`. `should_transition = (result == MET)`
     (gates.py:273). Adding the leaf to `all_of` makes `result=NOT_MET` until approved ⇒ no write.
     **No code change needed at this site** — wiring the pack does it.
  2. **CLI `run_phase_set`** — phase.py:124. Non-`force` path already calls `_forward_gate_blocks`
     (phase.py:84) which evaluates the packaged `exit_gates` deterministically (`lm_client=None`)
     and blocks on `NOT_MET`. The new leaf is a deterministic predicate ⇒ it blocks here for free.
     **The only hole is `force=True`** (phase.py:147 `if not force and ...`) which skips
     `_forward_gate_blocks` entirely — closed in §3 below.
- **`raw_prose`** (the orientation block the proxy composes from the corpus) is the *only*
  re-embed-gated artifact. The present-and-STOP rewrite (§4) needs the shared re-embed to reach
  composed blocks; the enforcement above does not depend on it.

---

## 1. New predicate `approval_recorded` (embed-free) — `signals/predicates.py`

Mirror of `eval_artifact_newer_than` (predicates.py:238) but with the marker path **derived from
phase**, not passed as a `path` arg. Using `path` would make `prefilter._extract_gate_paths`
(prefilter.py:22-35) collect `.agentalloy/approved/<phase>` and emit the misleading "produce its
exit artifact" advisory (gates.py:305-311). Arg name is **`since`** (the exit-artifact glob) ⇒
`_extract_gate_paths` ignores it (no `path` key) ⇒ no false advisory. `since` is also already a
sibling gate path so it adds no new derived prose token via `invariants.derive_invariants`.

Add three symbols near `eval_artifact_newer_than`:

```python
# --- approval gate -------------------------------------------------------

# Forward routes that always require a human approval marker.
_ALWAYS_APPROVAL_PHASES = ("spec", "design")


def approval_required(phase: str | None) -> bool:
    """True when leaving *phase* requires a recorded human approval.

    spec/design: always. sdd-fast: behind SDD_FAST_REQUIRE_APPROVAL (default OFF).
    Everything else (intake, build, qa, ship): never.
    """
    if phase in _ALWAYS_APPROVAL_PHASES:
        return True
    if phase == "sdd-fast":
        try:
            from agentalloy.config import get_settings  # lazy, like gates.py

            return bool(get_settings().sdd_fast_require_approval)
        except Exception:
            return False
    return False


def approval_marker_path(project_root: Path, phase: str) -> Path:
    return project_root / ".agentalloy" / "approved" / phase


def eval_approval_recorded(args: dict[str, Any], ctx: PredicateContext) -> PredicateResult:
    phase = args.get("phase") or ctx.current_phase
    if phase is None:
        return PredicateResult.UNKNOWN
    if not approval_required(phase):
        return PredicateResult.MET  # route is not approval-gated → satisfied
    marker = approval_marker_path(ctx.project_root, str(phase))
    if not marker.is_file():
        return PredicateResult.NOT_MET  # awaiting approval
    since_pattern = args.get("since", "")
    if not since_pattern:
        return PredicateResult.MET  # existence-only marker
    artifacts = _glob_files(ctx.project_root, since_pattern)
    if not artifacts:
        return PredicateResult.NOT_MET  # nothing produced → nothing approvable
    try:
        marker_mtime = marker.stat().st_mtime
        artifact_mtime = max(f.stat().st_mtime for f in artifacts if f.is_file())
        # >= (not strict >) tolerates same-second granularity; staleness is only
        # when the exit artifact is edited *after* approval.
        return PredicateResult.MET if marker_mtime >= artifact_mtime else PredicateResult.NOT_MET
    except OSError:
        return PredicateResult.UNKNOWN
```

Register in the `PREDICATES` dict (predicates.py:404), e.g. after `"artifact_newer_than"`:

```python
    "approval_recorded": eval_approval_recorded,
```

**Decision pinned:** marker comparison is `>=` (same-second tolerant). Strict `>` would spuriously
flag a stale approval when marker and artifact land in the same coarse mtime tick.

---

## 2. Awaiting-approval advisory — `signals/gates.py`

`decide_transition`'s missing-path advisory block (gates.py:282-311) only speaks for **missing
`path` globs**. `approval_recorded` has no `path` and the exit artifacts exist, so a complete-but-
unapproved phase would block **silently**. Attach an advisory on the leaf eval instead.

In `evaluate_node` (gates.py:174-189), today advisory is set only for `artifact_completeness`
(pre-result). Compute the approval advisory **after** `result` is known:

```python
    advisory: str | None = None
    if predicate_name == "artifact_completeness":
        advisory = _build_completeness_advisory(args, ctx)

    try:
        result = _evaluate_single(predicate_name, args, ctx, lm_client, qwen_calls)
    except ValueError:
        result = PredicateResult.UNKNOWN

    if predicate_name == "approval_recorded" and result == PredicateResult.NOT_MET:
        advisory = _build_approval_advisory(ctx)

    eval_record = GateEvaluation(...)  # unchanged
```

Add the helper (near `_build_completeness_advisory`):

```python
def _build_approval_advisory(ctx: PredicateContext) -> str:
    phase = ctx.current_phase or "this phase"
    return (
        f"'{phase}' is complete and awaiting human approval. PRESENT the work in full and STOP; "
        f"run `agentalloy approve {phase}` only after the user explicitly approves (re-run it if the "
        f"exit artifact changed after the last approval)."
    )
```

`decide_transition` already lifts leaf advisories into `decision.advisories` (gates.py:271), and the
proxy injects when advisories are non-empty (proxy_signal.py:572). So the present-and-STOP nudge is
surfaced to the agent at the live proxy *and* via `phase set` stderr.

---

## 3. Close the `--force` hole — `install/subcommands/phase.py`

Add an **unconditional** approval pre-check that runs even under `force`, before the existing
`if not force` block (phase.py:147).

New helper (alongside `_forward_gate_blocks`, phase.py:84):

```python
def _approval_gate_blocks(current: str, target: str, root: Path) -> tuple[bool, list[str]]:
    """Approval is the human checkpoint --force must NOT bypass.

    Only forward, approval-gated routes are checked. Evaluates the deterministic
    approval_recorded predicate directly (embed-free). NOT_MET → block + advisory.
    """
    from agentalloy.signals.gates import _PHASE_GRAPH  # noqa: PLC0415
    from agentalloy.signals.predicates import (  # noqa: PLC0415
        PredicateContext,
        PredicateResult,
        approval_required,
        eval_approval_recorded,
    )

    if target != _PHASE_GRAPH.get(current):
        return False, []  # backward / bail / non-linear
    if not approval_required(current):
        return False, []
    ctx = PredicateContext(project_root=root, current_phase=current)
    since = _APPROVAL_SINCE.get(current, "")
    result = eval_approval_recorded({"since": since}, ctx)
    if result != PredicateResult.NOT_MET:
        return False, []
    return True, [
        f"'{current}' requires human approval before advancing to '{target}'. "
        f"Run `agentalloy approve {current}` once the user has approved."
    ]
```

Add the per-phase exit-artifact glob map at module level (shared with `approve.py`, §5):

```python
_APPROVAL_SINCE = {
    "spec": "docs/spec/*.md",
    "design": "docs/design/**/*.md",
    "sdd-fast": "docs/fast/*.md",
}
```

Wire into `run_phase_set` (phase.py:144-155) **above** the `if not force` block:

```python
    existing = _read_phase(root)
    current = existing.get("phase") if existing else None

    # Human-approval gate is unforgeable-by-force: --force bypasses only
    # artifact-completeness, never the human checkpoint.
    if current and current != phase:
        appr_blocked, appr_adv = _approval_gate_blocks(current, phase, root)
        if appr_blocked:
            return {
                "phase": current,
                "blocked": True,
                "target": phase,
                "advisories": appr_adv,
                "reason": "approval",
            }

    if not force and current and current != phase:
        blocked, advisories = _forward_gate_blocks(current, phase, root)
        ...
```

`_run_set` (phase.py:255) already prints `result["advisories"]` and returns 1 on `blocked`, so the
new block path needs no printer change (optionally branch the headline on `reason == "approval"`).

**Note:** when `approve` itself calls `run_phase_set(next)` (§5) the marker already exists ⇒
`_approval_gate_blocks` returns `(False, [])` ⇒ no self-block.

---

## 4. New CLI `agentalloy approve <phase>` — `install/subcommands/approve.py` (NEW)

Mirror the `phase.py` subcommand module shape (`add_parser` + `_run` + `set_defaults(func=...)`).

```python
"""``approve`` subcommand — record a human approval marker and auto-advance."""
from __future__ import annotations

import argparse, hashlib, os, sys, uuid, contextlib
from datetime import UTC, datetime
from pathlib import Path

_APPROVABLE = ("spec", "design", "sdd-fast")
_EXIT_ARTIFACT_GLOB = {
    "spec": "docs/spec/*.md",
    "design": "docs/design/**/*.md",
    "sdd-fast": "docs/fast/*.md",
}


def _digest(root: Path, glob: str) -> str:
    files = sorted(p for p in root.glob(glob) if p.is_file())
    h = hashlib.sha256()
    for p in files:
        h.update(str(p.relative_to(root)).encode())
        h.update(b":")
        h.update(hashlib.sha256(p.read_bytes()).hexdigest().encode())
        h.update(b"\n")
    return h.hexdigest()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def run_approve(phase: str, root: Path | None = None, approver: str | None = None) -> dict:
    from agentalloy.install.state import _repo_root  # pyright: ignore[reportPrivateUsage]
    from agentalloy.install.subcommands.phase import run_phase_set, _read_phase
    from agentalloy.signals.gates import _PHASE_GRAPH  # pyright: ignore[reportPrivateUsage]
    from agentalloy.signals.predicates import approval_marker_path

    root = root or _repo_root()
    existing = _read_phase(root)
    current = existing.get("phase") if existing else None
    if current != phase:
        return {"ok": False, "error": f"current phase is '{current}', not '{phase}'"}

    glob = _EXIT_ARTIFACT_GLOB[phase]
    if not any(p.is_file() for p in root.glob(glob)):
        return {"ok": False, "error": f"no exit artifact at '{glob}' to approve"}

    approver = approver or os.environ.get("USER") or "unknown"
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    sha = _digest(root, glob)
    marker = approval_marker_path(root, phase)
    _atomic_write(
        marker,
        f'approver: {approver}\napproved_at: "{now}"\nartifact_sha256: {sha}\n',
    )

    nxt = _PHASE_GRAPH.get(phase, phase)
    advanced = run_phase_set(nxt, root=root)  # marker now exists → approval gate passes
    return {"ok": True, "phase": phase, "approver": approver, "marker": str(marker),
            "advanced": advanced}
```

Argparse: `approve <phase>` with `choices=_APPROVABLE`, `--approver`, plus the shared
`--project-root` flag (copy `_add_project_root_flag`/`_resolve_root` from phase.py or import them).
`_run` prints the marker path + whether it advanced or was itself blocked
(`advanced.get("blocked")` → surface those advisories, e.g. design missing a build contract: the
approval is recorded but the forward step still needs the artifact-completeness gate).

Register in `install/__main__.py`: add `approve` to the import block (line 21-65) and to
`_SUBCOMMANDS` (line 76-124), next to `phase`.

**Marker format** is the same flat `key: value` shape `phase.py:_read_phase` already parses, so the
file is round-trippable. `artifact_sha256` gives telemetry/post-hoc detectability of which artifact
state was approved (cooperative-agent trust model, not hard unforgeability — consistent with the
existing `--force` parity).

---

## 5. Config knob — `config.py`

Add to `Settings` (config.py:50, bare-name env mapping ⇒ `SDD_FAST_REQUIRE_APPROVAL`):

```python
    # Human approval gate on the sdd-fast lane (spec/design are always gated).
    sdd_fast_require_approval: bool = False
```

If a config-consistency test enumerates `Settings` fields (cross-cutting risk #7), add this knob to
its allowlist.

---

## 6. Pack wiring — `_packs/sdd/{sdd-spec,sdd-design,sdd-fast}.yaml`

**(a) Exit-gate leaf** — append to each pack's `exit_gates.all_of`:

- `sdd-spec-and-scoping.yaml` (after the `artifact_contains docs/spec/*.md` block, ~line 28):
  ```yaml
      - approval_recorded:
          since: docs/spec/*.md
  ```
- `sdd-design-and-planning.yaml` (after the final `artifact_exists .agentalloy/contracts/build/*.md`, ~line 46):
  ```yaml
      - approval_recorded:
          since: docs/design/**/*.md
  ```
- `sdd-fast.yaml` (after `artifact_exists tests/**/*.py`, ~line 32):
  ```yaml
      - approval_recorded:
          since: docs/fast/*.md
  ```
  Inert by default: `approval_required("sdd-fast")` returns `False` ⇒ predicate returns `MET` ⇒ no
  block until `SDD_FAST_REQUIRE_APPROVAL=true`.

**Decision pinned:** design `since` is `docs/design/**/*.md` (the three design docs + any extra
design files), **not** the build contracts — emitting/editing a build contract during build should
not re-stale the design approval.

**(b) `prose_invariants`** — the agent-facing forward command changes from `phase set <next>` to
`approve <phase>`:

- `sdd-spec-and-scoping.yaml` (line 18-19): replace `"agentalloy phase set design"` →
  `"agentalloy approve spec"`.
- `sdd-design-and-planning.yaml` (line 19-21): replace `"agentalloy phase set build"` →
  `"agentalloy approve design"`; keep `"agentalloy task next"`.
- `sdd-fast.yaml`: unchanged (default OFF keeps `phase set qa` as the forward verb).

**(c) `raw_prose` present-and-STOP rewrite** (the corpus-gated rider — fold into the shared
re-embed):

- `sdd-spec-and-scoping.yaml` §5 (lines 129-135) before→after:
  - **before:** `Then advance yourself — don't wait to be told. The moment the spec is written and
    unambiguous, run` `agentalloy phase set design` `and carry straight on into design.`
  - **after:** `Then PRESENT the spec in full and STOP — do not advance yourself. The forward jump
    is a human checkpoint: only once the user explicitly approves do you run` `agentalloy approve spec`
    `(it records the approval and carries you into design). The phase guard refuses the jump until
    the spec exists with its required sections AND a human approval is recorded, so a premature`
    `phase set` `or` `approve` `just tells you what's missing.`
- `sdd-design-and-planning.yaml` §6 (lines 164-174) before→after: same shape — replace
  `run` `agentalloy phase set build` with PRESENT-and-STOP + `agentalloy approve design`; keep the
  `task next` worklist sentence and the "verify every task has a matching contract" check.

The §G todo also edits `sdd-design-and-planning.yaml` (build-contract density §6 prose). **Coordinate
the §6 rewrite with §G** so both land in one pack version — the approval rewrite is a single-sentence
swap in the same §6 paragraph §G is tightening.

---

## 7. Tests

| File | Test | Asserts |
|------|------|---------|
| `tests/test_predicates.py` | `test_approval_recorded_no_marker_not_met` | spec phase, no marker → NOT_MET |
| | `test_approval_recorded_marker_postdates_met` | marker mtime ≥ artifact → MET |
| | `test_approval_recorded_stale_not_met` | touch artifact after marker → NOT_MET |
| | `test_approval_recorded_no_phase_unknown` | `current_phase=None` → UNKNOWN |
| | `test_approval_recorded_route_not_required_met` | phase=`build` → MET (no marker needed) |
| | `test_approval_recorded_sdd_fast_flag` | OFF→MET, `SDD_FAST_REQUIRE_APPROVAL=1`→NOT_MET |
| `tests/test_gates.py` | `test_decide_transition_blocked_until_approval` | artifacts present, no marker → `should_transition=False`; marker → True |
| | `test_decide_transition_awaiting_approval_advisory` | advisory mentions `approve spec` when unapproved |
| `tests/install/test_phase_cli.py` | `test_force_does_not_bypass_approval` | `run_phase_set('design', force=True)` → `blocked, reason='approval'` with no marker |
| | `test_force_bypasses_completeness_not_approval` | marker present + spec doc missing sections + force → advances |
| `tests/install/test_approve_cli.py` (NEW) | `test_approve_writes_marker_and_advances` | marker has approver/approved_at/artifact_sha256; phase→design |
| | `test_approve_refuses_without_exit_artifact` | no `docs/spec/*.md` → error, no marker, phase unchanged |
| | `test_approve_wrong_current_phase_errors` | current=design, `approve spec` → error |
| `tests/test_proxy_signal.py` | `test_completion_unapproved_no_transition` | completion-intent turn, no marker → phase unchanged + advisory injected |
| | `test_completion_approved_transitions` | marker present → `_write_phase_atomic` fires |
| config-consistency (existing gate test file) | `test_approval_recorded_in_spec_and_design_gates` | `exit_gates_for_phase('spec'/'design')` contains an `approval_recorded` leaf; predicate in `PREDICATES` |
| invariants test file | `test_prose_override_dropping_approve_rejected` | override prose without `agentalloy approve spec` → shipped prose retained (`overlay_prose` returns missing token) |

---

## 8. Sequencing & batch notes

1. predicates.py (predicate + `approval_required` + marker path + registry) — no deps.
2. gates.py advisory — depends on (1) for the predicate name.
3. config.py knob — independent.
4. phase.py force carve-out — depends on (1).
5. approve.py + __main__ registration — depends on (1) and phase.py.
6. Pack `exit_gates` + `prose_invariants` edits — depend on (1) (predicate must exist before the
   container image / wheel ships a gate referencing it, else `evaluate_predicate` raises `ValueError`
   → caught as UNKNOWN in `evaluate_node`, which would *silently not block*; ship (1) with (6)).
7. Pack `raw_prose` rewrite — ride the §E/§F/§G/§8 corpus re-embed.

**Batch classification:** CODE. The enforcement (predicate, CLI, force carve-out, config, advisory,
exit-gate wiring) needs **no embed** — it reads packaged YAML DB-free and runs from the wheel/image.
`needs_reembed=true` is set **only** because the §6 `raw_prose` present-and-STOP rewrite should be
folded into the corpus rebuild that the other items are already triggering today; the gate itself
works the instant the new wheel/image is in place.

**File-conflict watch:** `sdd-design-and-planning.yaml` and `sdd-spec-and-scoping.yaml` are also
edited by §G (build-contract density). Land the §H pack edits (gate leaf + prose_invariants swap)
and §G's §6 tightening in one coordinated pack version to avoid a merge stomp.
