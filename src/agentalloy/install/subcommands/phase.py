"""``phase`` subcommand — the current SDD phase.

Phase lives in the ``sdd_state`` store, one row per repo; there is no
``.agentalloy/phase`` file and no fallback to one.  Every verb here therefore
needs a reachable store: in-process when the service runs this code itself,
over HTTP otherwise (see :mod:`agentalloy.install.subcommands._state`).

Commands:
    agentalloy phase             — print current phase
    agentalloy phase set <phase> — advance (or move) the phase
    agentalloy phase clear       — remove the phase row
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentalloy.api.state_client import StateClientError
from agentalloy.install.subcommands._state import fail_on_state_error, phase_access
from agentalloy.signals.gates import evaluate_phase_gate as _evaluate_phase_gate

logger = logging.getLogger(__name__)

# "intake" is the entry phase: a freshly-wired repo starts here so the intake
# workflow (intent interview) composes on the first prompt, then hands off to
# "spec" (see signals.gates._PHASE_GRAPH).
VALID_PHASES = ("intake", "spec", "design", "build", "qa", "ship", "sdd-fast", "add-skill")

SCHEMA_VERSION = 1

# Per-phase exit-artifact glob used to detect a *stale* approval (artifact edited
# after the marker was written). Shared in spirit with approve.py's map. Phases
# absent here are existence-only / not approval-gated.
#
# spec/design moved to the artifact store (see specs/final_migration.md) — their
# staleness check is a store-side name_glob, not a filesystem glob, matched by
# ``_APPROVAL_STORE_NAME_GLOB`` below. sdd-fast/add-skill are unmigrated and keep
# the disk glob.
_APPROVAL_SINCE = {
    "sdd-fast": "docs/fast/*.md",
    "add-skill": ".agentalloy/custom-skills/**/*.yaml",
}
_APPROVAL_STORE_NAME_GLOB = {
    "spec": "*.md",
    "design": "*.md",
}


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_phase_get(root: Path | None = None) -> dict[str, Any]:
    """The current phase for *root*, read from the store.

    ``phase: None`` means the repo has no phase recorded — a legitimate state
    for a freshly wired repo.  An unreachable store exits non-zero instead of
    reporting that, so an outage cannot masquerade as a fresh repo.
    """
    from agentalloy.install.state import _repo_root  # pyright: ignore[reportPrivateUsage]

    root = root or _repo_root()
    try:
        state = phase_access(root).read()
    except StateClientError as exc:
        fail_on_state_error(exc)
        raise  # unreachable — fail_on_state_error exits
    if state is None:
        return {"phase": None, "message": "No active phase"}
    return {
        "phase": state.phase,
        "started_at": state.started_at,
        "last_updated": state.last_updated,
        "workflow": state.workflow,
    }


def _forward_gate_blocks(
    current: str, target: str, root: Path, store: Any
) -> tuple[bool, list[str]]:
    """Whether a *forward* ``phase set`` should be refused, with advisories.

    A transition is "forward" only when ``target`` is the next phase in the
    linear SDD graph (``_PHASE_GRAPH[current]``). Backward routes (``qa → build``,
    ``design → spec``), bail routes (``sdd-fast → spec``), and the ship→intake
    reset are not forward, so they never gate — they return ``(False, [])``.

    For a forward target we evaluate the *current* phase's packaged ``exit_gates``
    deterministically (``lm_client=None``): only a hard ``NOT_MET`` blocks. An
    embed-dependent predicate yields ``UNKNOWN`` and never blocks, so the guard
    enforces exactly the cheap, certain checks (the exit artifact exists / has its
    required sections) and stays out of the way of everything it can't be sure of.
    """
    from agentalloy.signals.gates import (  # noqa: PLC0415
        _PHASE_GRAPH,  # pyright: ignore[reportPrivateUsage]
        decide_transition,
        evaluate_node,
    )
    from agentalloy.signals.predicates import PredicateContext, PredicateResult  # noqa: PLC0415
    from agentalloy.signals.skill_loader import exit_gates_for_phase  # noqa: PLC0415

    if target != _PHASE_GRAPH.get(current):
        return False, []  # backward / bail / non-linear → unguarded

    gate_spec = exit_gates_for_phase(current)
    if not gate_spec:
        return False, []  # no packaged gate for this phase → nothing to enforce

    ctx = PredicateContext(project_root=root, current_phase=current, store=store)
    result, _ = evaluate_node(gate_spec, ctx, lm_client=None, qwen_calls=[0])
    if result != PredicateResult.NOT_MET:
        return False, []  # MET or UNKNOWN → allow

    # Reuse decide_transition purely for its advisory text (which exit artifact
    # is missing / misplaced). It re-evaluates deterministically (lm_client=None).
    decision = decide_transition(current, gate_spec, ctx, lm_client=None)
    return True, decision.advisories


def _approval_gate_blocks(
    current: str, target: str, root: Path, store: Any
) -> tuple[bool, list[str]]:
    """Whether a forward ``phase set`` must be refused for lack of human approval.

    Approval is the human checkpoint that ``--force`` must NOT bypass (``--force``
    only waives artifact-completeness). Only forward, approval-gated routes are
    checked; everything else returns ``(False, [])``. Evaluates the deterministic
    ``approval_recorded`` predicate directly (embed-free): only a hard ``NOT_MET``
    (no marker, or the exit artifact changed after approval) blocks.

    When the exit artifact doesn't exist yet there is nothing to approve, so we
    defer to the completeness gate (``_forward_gate_blocks``) to drive the
    "produce the exit artifact" message — mirroring the packaged ``exit_gates``,
    where ``approval_recorded`` sits *after* ``artifact_exists`` in the ``all_of``
    and is only reached once the artifact is on disk.
    """
    from agentalloy.signals.gates import (  # noqa: PLC0415
        _PHASE_GRAPH,  # pyright: ignore[reportPrivateUsage]
    )
    from agentalloy.signals.predicates import (  # noqa: PLC0415
        PredicateContext,
        PredicateResult,
        approval_required,
        eval_approval_recorded,
    )

    if target != _PHASE_GRAPH.get(current):
        return False, []  # backward / bail / non-linear → unguarded
    if not approval_required(current):
        return False, []

    ctx = PredicateContext(project_root=root, current_phase=current, store=store)
    if current in _APPROVAL_STORE_NAME_GLOB:
        name_glob = _APPROVAL_STORE_NAME_GLOB[current]
        rows = store.list_artifacts(current, name_glob=name_glob) if store is not None else []
        if not rows:
            return False, []  # nothing produced yet → completeness gate handles it
        result = eval_approval_recorded({"since_name_glob": name_glob}, ctx)
    else:
        since = _APPROVAL_SINCE.get(current, "")
        if since and not any(p.is_file() for p in root.glob(since)):
            return False, []  # nothing produced yet → completeness gate handles it
        result = eval_approval_recorded({"since": since}, ctx)
    if result != PredicateResult.NOT_MET:
        return False, []  # MET or UNKNOWN → allow
    return True, [
        f"'{current}' requires human approval before advancing to '{target}'. "
        f"Run `agentalloy approve {current}` once the user has approved."
    ]


def run_phase_set(phase: str, root: Path | None = None, force: bool = False) -> dict[str, Any]:
    """Set or update the current phase.

    A *forward* transition (the next phase in the linear SDD graph) is gated on
    the current phase's deterministic exit gates: if the exit artifact isn't on
    disk, the write is refused and the returned dict carries ``blocked=True`` plus
    advisories naming what's missing. ``force=True`` bypasses that completeness
    gate. Backward, bail, and reset transitions are never gated.

    The human-approval gate is separate and *unforgeable-by-force*: leaving an
    approval-gated phase (spec/design, plus sdd-fast when enabled) without a
    recorded approval marker is refused with ``reason="approval"`` even under
    ``force``. Backward, bail, and reset transitions are never gated.

    The write goes to the state store — in-process when this runs inside the
    service, over HTTP otherwise.  There is no file-mirror path left: a store
    that cannot be reached stops the command instead of writing somewhere the
    service will never read.
    """
    from agentalloy.install.state import _repo_root  # pyright: ignore[reportPrivateUsage]

    root = root or _repo_root()

    if phase not in VALID_PHASES:
        print(
            f"Error: invalid phase '{phase}'. Valid phases: {', '.join(VALID_PHASES)}",
            file=sys.stderr,
        )
        sys.exit(1)

    access = phase_access(root)
    try:
        existing = access.read()
    except StateClientError as exc:
        fail_on_state_error(exc)
        raise  # unreachable
    current = existing.phase if existing else None
    gate_store = access.contracts_handle()

    # Unified gate evaluation (slice 09: route evaluates, CLI delegates).
    # evaluate_phase_gate checks: always-approval phases, approval gate,
    # forward gate (artifact completeness).  --force maps to override=True
    # which bypasses the forward gate but NOT the approval gate.
    verdict = _evaluate_phase_gate(current, phase, root, force, gate_store)
    if verdict is not None:
        return {
            "phase": current or phase,
            "blocked": True,
            "target": phase,
            "advisories": verdict.get("advisories", []),
            "reason": verdict.get("result", "not_met"),
        }

    # Record who caused a real transition, mirroring the proxy's
    # `skill_loader._write_phase_atomic` — lets a *different* session's next
    # turn recognize the phase changed out from under it (see
    # `proxy_signal._boundary_confirm_directives`'s "swept" case). An idempotent
    # `phase set` to the same phase preserves the prior actor unchanged (the
    # store enforces that, not this call site). No `CLAUDE_CODE_SESSION_ID` (a
    # bare terminal invocation, not run from within a tracked session) records
    # nothing — ambiguous, not attributable.
    from agentalloy.signals.skill_loader import cli_session_key  # noqa: PLC0415

    try:
        # `mode`/`free_since` are deliberately not passed: omitting them carries
        # the stored pair forward, so a phase set never drops the repo out of
        # free-flow. Only `agentalloy flow` sets them.
        access.write(phase, actor=cli_session_key() or None, override=force)
        state = access.read()
    except StateClientError as exc:
        fail_on_state_error(exc)
        raise  # unreachable

    data: dict[str, Any] = {
        "phase": phase,
        "started_at": state.started_at if state else _now_iso(),
        "last_updated": state.last_updated if state else _now_iso(),
        "workflow": f"sdd-{phase}",
    }
    if state and state.mode:
        data["mode"] = state.mode
    if state and state.free_since:
        data["free_since"] = state.free_since
    if state and state.transitioned_by:
        data["transitioned_by"] = state.transitioned_by

    # On a real transition, SEED the work-item cursor to the new phase's first work-item
    # (filename order) so "which task is current" is reliably set — the single source of
    # truth both the proxy and the codify gate read. A phase with no contracts clears it.
    # Mirrors the proxy auto-advance path in skill_loader._write_phase_atomic (B2).
    # Auto-archive on ship: the user-confirmed ship→intake reset ends a work
    # cycle, so sweep the just-completed cycle's live contracts into
    # archive/<phase>/ before the next cycle starts writing into active/.
    if current == "ship" and phase == "intake":
        from agentalloy.api.state_client import StateClient
        from agentalloy.contracts import apply_contracts_migration, plan_archive

        try:
            client = StateClient()
            client.archive_all()
        except Exception:
            logger.warning("archive_all failed — store archiving skipped")
        apply_contracts_migration(plan_archive(root))

    if current != phase:
        from agentalloy.contracts import first_workitem_id
        from agentalloy.signals.skill_loader import (  # pyright: ignore[reportPrivateUsage]
            _clear_all_cursors,
            _write_cursor_atomic,
        )

        # Clear stale scoped cursors, then seed the shared cursor for the new phase.
        _clear_all_cursors(root)
        seed = first_workitem_id(root, phase)
        if seed:
            _write_cursor_atomic(root, seed)
    return {**data, "blocked": False}


def run_phase_clear(root: Path | None = None) -> dict[str, Any]:
    """Delete the phase row, leaving *root* genuinely phase-less."""
    from agentalloy.install.state import _repo_root  # pyright: ignore[reportPrivateUsage]

    root = root or _repo_root()
    access = phase_access(root)
    try:
        existing = access.read()
        access.clear()
    except StateClientError as exc:
        fail_on_state_error(exc)
        raise  # unreachable
    message = "Phase cleared" if existing else "No phase to clear"
    return {"message": message, "phase": None}


# ---------------------------------------------------------------------------
# Subcommand interface
# ---------------------------------------------------------------------------


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    p: argparse.ArgumentParser = subparsers.add_parser(
        "phase",
        help="Manage the current SDD phase (get, set, clear).",
    )
    _add_project_root_flag(p)  # for the default (get) action
    sub = p.add_subparsers(dest="phase_action")

    # Explicit `phase get` — the natural read verb agents reach for. Bare
    # `phase` also runs get (the default below), but `get` must be a real
    # subcommand or argparse rejects it as an invalid choice.
    p_get = sub.add_parser("get", help="Print the current phase")
    _add_project_root_flag(p_get)
    p_get.set_defaults(func=_run_get)

    p_set = sub.add_parser("set", help="Set the current phase")
    p_set.add_argument(
        "phase",
        choices=VALID_PHASES,
        help="Phase to set: intake, spec, design, build, qa, ship, sdd-fast, add-skill",
    )
    p_set.add_argument(
        "--force",
        action="store_true",
        help="Advance even if the current phase's exit gate isn't met.",
    )
    _add_project_root_flag(p_set)
    p_set.set_defaults(func=_run_set)

    p_clear = sub.add_parser("clear", help="Clear the current phase")
    _add_project_root_flag(p_clear)
    p_clear.set_defaults(func=_run_clear)

    # Default action (no subcommand) = get
    p.set_defaults(func=_run_get)


def _add_project_root_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--project-root",
        default=None,
        help=(
            "Repo whose phase row to read/write. Default: auto-detect from cwd (stops at $HOME)."
        ),
    )


def _resolve_root(args: argparse.Namespace) -> Path | None:
    """Explicit --project-root wins; otherwise None defers to _repo_root()."""
    pr = getattr(args, "project_root", None)
    return Path(pr).expanduser().resolve() if pr else None


def _run_get(args: argparse.Namespace) -> int:
    result = run_phase_get(root=_resolve_root(args))
    print(f"Phase: {result.get('phase', 'none')}")
    if result.get("started_at"):
        print(f"Started: {result['started_at']}")
    if result.get("last_updated"):
        print(f"Updated: {result['last_updated']}")
    if result.get("workflow"):
        print(f"Workflow: {result['workflow']}")
    return 0


def _run_set(args: argparse.Namespace) -> int:
    result = run_phase_set(
        args.phase, root=_resolve_root(args), force=getattr(args, "force", False)
    )
    if result.get("blocked"):
        if result.get("reason") == "approval":
            # The human checkpoint --force cannot bypass: don't suggest --force.
            print(
                f"Refusing to advance {result['phase']} → {result['target']}: "
                f"awaiting human approval.",
                file=sys.stderr,
            )
            for advisory in result.get("advisories", []):
                print(f"  {advisory}", file=sys.stderr)
            return 1
        print(
            f"Refusing to advance {result['phase']} → {result['target']}: "
            f"the current phase's exit gate isn't met.",
            file=sys.stderr,
        )
        for advisory in result.get("advisories", []):
            print(f"  {advisory}", file=sys.stderr)
        print(
            "  Finish the exit artifact, or pass --force once you've confirmed the work is done.",
            file=sys.stderr,
        )
        return 1
    print(f"Phase set to: {result['phase']}")
    return 0


def _run_clear(args: argparse.Namespace) -> int:
    result = run_phase_clear(root=_resolve_root(args))
    print(result["message"])
    return 0
