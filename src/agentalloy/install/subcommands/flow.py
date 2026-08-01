"""``flow`` subcommand — free-flow mode management.

Free-flow is a per-repo mode for sessions with no specific task in mind: it
pauses ALL workflow steering (orientation, banners, exit gates, phase
transitions, intake) while keeping domain-skill composition. It rides the same
per-repo ``phase`` row the phase machine uses, as an optional ``mode: free`` +
``free_since: <iso>`` pair — entering free-flow never changes the ``phase``
value, so resume returns to exactly the prior phase.

Like ``phase set``, these are deterministic per-repo writes (no LM involvement)
and the phase row is SHARED by every concurrent session in the repo:
``flow free`` / ``flow resume`` affect all of them, not just yours.

Commands:
    agentalloy flow free    — pause workflow steering (idempotent)
    agentalloy flow resume  — resume workflow at the prior phase (idempotent)
    agentalloy flow status  — current mode, phase, and since-when
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from agentalloy.api.state_client import StateClientError
from agentalloy.install.subcommands._state import fail_on_state_error, phase_access
from agentalloy.install.subcommands.phase import _now_iso  # pyright: ignore[reportPrivateUsage]

# A repo that was never wired has no phase row; free-flow still works there —
# the row is created at the entry phase so resume lands where a fresh wire
# would (intake runs on the first post-resume request).
_DEFAULT_PHASE = "intake"


def run_flow_free(root: Path | None = None) -> dict[str, Any]:
    """Enter free-flow: set ``mode: free`` + ``free_since`` on the phase row.

    Idempotent — already-free returns ``changed=False`` with the original
    ``free_since``. Never touches the ``phase`` value. Affects every session in
    the repo (the phase row is per-repo shared state).

    ``mode`` and ``free_since`` are written as their own fields.  They used to
    be smuggled into the phase *name* (``"free-flow:design"``), which stored a
    phase no consumer recognises and forced the call to skip the posture
    rewrite so the bogus name would not clear the deny rules.
    """
    from agentalloy.install.state import _repo_root  # pyright: ignore[reportPrivateUsage]

    root = root or _repo_root()
    access = phase_access(root)
    try:
        state = access.read()
        phase = state.phase if state else _DEFAULT_PHASE
        if state is not None and (state.mode or "").lower() == "free":
            return {
                "phase": phase,
                "mode": "free",
                "free_since": state.free_since,
                "changed": False,
            }
        since = _now_iso()
        access.write(phase, mode="free", free_since=since)
        # Immediately update Tier A harness configs so free-flow writes take effect.
        # The posture rewrite reads the flow state to determine if deny rules should be active.
        try:
            from agentalloy.install.subcommands.wire_harness import (
                rewrite_enforcement_posture,
            )

            rewrite_enforcement_posture(root, phase)
        except Exception:
            pass  # soft: a posture failure must not block free-flow
    except StateClientError as exc:
        fail_on_state_error(exc)
        raise  # unreachable
    return {"phase": phase, "mode": "free", "free_since": since, "changed": True}


def run_flow_resume(root: Path | None = None) -> dict[str, Any]:
    """Leave free-flow: clear ``mode``/``free_since``, restoring the exact prior
    phase. Idempotent — a repo not in free-flow returns ``changed=False``.

    Also clears the daily-reminder marker so a later ``flow free`` starts a
    fresh 24h clock. The announced marker is deliberately left alone: while
    free, it holds the free sentinel, which mismatches every real phase — so
    the next proxy request re-orients (intake included) as a first request.
    Affects every session in the repo.
    """
    from agentalloy.install.state import _repo_root  # pyright: ignore[reportPrivateUsage]
    from agentalloy.signals.skill_loader import _clear_state  # pyright: ignore[reportPrivateUsage]

    root = root or _repo_root()
    access = phase_access(root)
    try:
        state = access.read()
        phase = state.phase if state else _DEFAULT_PHASE
        if state is None or (state.mode or "").lower() != "free":
            return {"phase": phase, "mode": "workflow", "changed": False}
        # Empty strings *clear* the pair; None would carry it forward.
        access.write(phase, mode="", free_since="")
    except StateClientError as exc:
        fail_on_state_error(exc)
        raise  # unreachable
    _clear_state(root, "free-reminded")
    return {"phase": phase, "mode": "workflow", "changed": True}


def run_flow_status(root: Path | None = None) -> dict[str, Any]:
    """Current flow mode, phase, and (when free) since-when."""
    from agentalloy.install.state import _repo_root  # pyright: ignore[reportPrivateUsage]

    root = root or _repo_root()
    try:
        state = phase_access(root).read()
    except StateClientError as exc:
        fail_on_state_error(exc)
        raise  # unreachable
    mode = "free" if state is not None and (state.mode or "").lower() == "free" else "workflow"
    return {
        "phase": state.phase if state else None,
        "mode": mode,
        "free_since": (state.free_since if state else None) if mode == "free" else None,
    }


# ---------------------------------------------------------------------------
# Subcommand interface
# ---------------------------------------------------------------------------


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    p: argparse.ArgumentParser = subparsers.add_parser(
        "flow",
        help="Free-flow mode: pause/resume workflow steering (free, resume, status).",
    )
    sub = p.add_subparsers(dest="flow_action")

    p_free = sub.add_parser("free", help="Pause workflow steering; keep skill composition")
    _add_project_root_flag(p_free)
    p_free.set_defaults(func=_run_free)

    p_resume = sub.add_parser("resume", help="Resume the workflow at the prior phase")
    _add_project_root_flag(p_resume)
    p_resume.set_defaults(func=_run_resume)

    p_status = sub.add_parser("status", help="Show the current flow mode")
    _add_project_root_flag(p_status)
    p_status.set_defaults(func=_run_status)

    # Default action (no subcommand) = status
    _add_project_root_flag(p)
    p.set_defaults(func=_run_status)


def _add_project_root_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--project-root",
        default=None,
        help=(
            "Repo whose phase row to read/write. Default: auto-detect from cwd (stops at $HOME)."
        ),
    )


def _resolve_root(args: argparse.Namespace) -> Path | None:
    pr = getattr(args, "project_root", None)
    return Path(pr).expanduser().resolve() if pr else None


def _run_free(args: argparse.Namespace) -> int:
    result = run_flow_free(root=_resolve_root(args))
    if result["changed"]:
        print(f"Free-flow enabled — workflow paused at phase '{result['phase']}'.")
        print("Domain skills still compose. Run `agentalloy flow resume` when ready.")
    else:
        print(
            f"Already in free-flow (since {result.get('free_since') or 'unknown'}); "
            f"workflow paused at phase '{result['phase']}'."
        )
    return 0


def _run_resume(args: argparse.Namespace) -> int:
    result = run_flow_resume(root=_resolve_root(args))
    if result["changed"]:
        print(f"Resuming workflow at phase '{result['phase']}'.")
    else:
        print(f"Not in free-flow; workflow already active at phase '{result['phase']}'.")
    return 0


def _run_status(args: argparse.Namespace) -> int:
    result = run_flow_status(root=_resolve_root(args))
    print(f"Mode: {result['mode']}")
    print(f"Phase: {result['phase'] or 'none'}")
    if result.get("free_since"):
        print(f"Free since: {result['free_since']}")
    return 0
