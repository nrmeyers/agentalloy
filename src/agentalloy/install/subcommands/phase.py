"""``phase`` subcommand — phase lock file management.

Manage the `.agentalloy/phase` YAML file that tracks the current
SDD phase for a project session.

Commands:
    agentalloy phase            — print current phase
    agentalloy phase set <phase> — write/update the phase lock file
    agentalloy phase clear      — remove the phase lock file
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# "intake" is the entry phase: a freshly-wired repo starts here so the intake
# workflow (intent interview) composes on the first prompt, then hands off to
# "spec" (see signals.gates._PHASE_GRAPH).
VALID_PHASES = ("intake", "spec", "design", "build", "qa", "ship", "sdd-fast", "add-skill")

SCHEMA_VERSION = 1

# Per-phase exit-artifact glob used to detect a *stale* approval (artifact edited
# after the marker was written). Shared in spirit with approve.py's map. Phases
# absent here are existence-only / not approval-gated.
_APPROVAL_SINCE = {
    "spec": "docs/spec/*.md",
    "design": "docs/design/**/*.md",
    "sdd-fast": "docs/fast/*.md",
    "add-skill": ".agentalloy/custom-skills/**/*.yaml",
}


def _phase_path(root: Path) -> Path:
    return root / ".agentalloy" / "phase"


def _read_phase(root: Path) -> dict[str, Any] | None:
    """Read and parse the phase lock file. Returns None if not found."""
    p = _phase_path(root)
    if not p.exists():
        return None
    # Simple YAML parser — no pyyaml dependency needed for this flat format.
    # Use partition on first colon only to handle values containing colons (e.g. ISO timestamps).
    data: dict[str, Any] = {}
    for line in p.read_text().splitlines():
        if ":" not in line or line.strip().startswith("#"):
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        data[key] = value
    return data if data else None


def _write_phase(data: dict[str, Any], root: Path) -> None:
    """Write the phase lock file, creating .agentalloy/ if needed."""
    p = _phase_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for key, value in data.items():
        if isinstance(value, str) and "T" in value:
            # ISO timestamp — quote it so YAML parsers read it as string
            lines.append(f'{key}: "{value}"')
        else:
            lines.append(f"{key}: {value}")
    p.write_text("\n".join(lines) + "\n")


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_phase_get(root: Path | None = None) -> dict[str, Any]:
    """Get the current phase from the lock file."""
    from agentalloy.install.state import _repo_root  # pyright: ignore[reportPrivateUsage]

    root = root or _repo_root()
    data = _read_phase(root)
    if data is None:
        return {"phase": None, "message": "No active phase"}
    return {
        "phase": data.get("phase"),
        "started_at": data.get("started_at"),
        "last_updated": data.get("last_updated"),
        "workflow": data.get("workflow"),
    }


def _forward_gate_blocks(current: str, target: str, root: Path) -> tuple[bool, list[str]]:
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

    ctx = PredicateContext(project_root=root, current_phase=current)
    result, _ = evaluate_node(gate_spec, ctx, lm_client=None, qwen_calls=[0])
    if result != PredicateResult.NOT_MET:
        return False, []  # MET or UNKNOWN → allow

    # Reuse decide_transition purely for its advisory text (which exit artifact
    # is missing / misplaced). It re-evaluates deterministically (lm_client=None).
    decision = decide_transition(current, gate_spec, ctx, lm_client=None)
    return True, decision.advisories


def _approval_gate_blocks(current: str, target: str, root: Path) -> tuple[bool, list[str]]:
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
    since = _APPROVAL_SINCE.get(current, "")
    if since and not any(p.is_file() for p in root.glob(since)):
        return False, []  # nothing produced yet → completeness gate handles it
    ctx = PredicateContext(project_root=root, current_phase=current)
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
    """
    from agentalloy.install.state import _repo_root  # pyright: ignore[reportPrivateUsage]

    root = root or _repo_root()

    if phase not in VALID_PHASES:
        print(
            f"Error: invalid phase '{phase}'. Valid phases: {', '.join(VALID_PHASES)}",
            file=sys.stderr,
        )
        sys.exit(1)

    existing = _read_phase(root)
    current = existing.get("phase") if existing else None

    # Human-approval gate runs unconditionally — --force waives only
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
        if blocked:
            return {
                "phase": current,
                "blocked": True,
                "target": phase,
                "advisories": advisories,
            }

    now = _now_iso()

    data: dict[str, Any] = {
        "phase": phase,
        "started_at": existing.get("started_at", now) if existing else now,
        "last_updated": now,
        "workflow": f"sdd-{phase}",
    }
    # Free-flow state (`agentalloy flow free`) rides the same file — a phase set
    # must not silently drop the repo out of free-flow.
    if existing:
        for key in ("mode", "free_since"):
            if key in existing:
                data[key] = existing[key]

    _write_phase(data, root)
    # On a real transition, SEED the work-item cursor to the new phase's first work-item
    # (filename order) so "which task is current" is reliably set — the single source of
    # truth both the proxy and the codify gate read. A phase with no contracts clears it.
    # Mirrors the proxy auto-advance path in skill_loader._write_phase_atomic (B2).
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
    """Remove the phase lock file."""
    from agentalloy.install.state import _repo_root  # pyright: ignore[reportPrivateUsage]

    root = root or _repo_root()
    p = _phase_path(root)
    if p.exists():
        p.unlink()
        return {"message": "Phase cleared", "phase": None}
    return {"message": "No phase to clear", "phase": None}


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
            "Repo directory to read/write the .agentalloy/phase file in. "
            "Default: auto-detect from cwd (stops at $HOME)."
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
