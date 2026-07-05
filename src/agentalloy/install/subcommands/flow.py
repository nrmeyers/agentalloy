"""``flow`` subcommand — free-flow mode management.

Free-flow is a per-repo mode for sessions with no specific task in mind: it
pauses ALL workflow steering (orientation, banners, exit gates, phase
transitions, intake) while keeping domain-skill composition. State lives in
the same per-repo ``.agentalloy/phase`` file the phase machine uses, as an
optional ``mode: free`` + ``free_since: <iso>`` pair — entering free-flow never
changes the ``phase`` value, so resume returns to exactly the prior phase.

Like ``phase set``, these are deterministic per-repo file edits (no LM
involvement) and the phase file is SHARED by every concurrent session in the
repo: ``flow free`` / ``flow resume`` affect all of them, not just yours.

Commands:
    agentalloy flow free    — pause workflow steering (idempotent)
    agentalloy flow resume  — resume workflow at the prior phase (idempotent)
    agentalloy flow status  — current mode, phase, and since-when
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from agentalloy.install.subcommands.phase import (  # pyright: ignore[reportPrivateUsage]
    _now_iso,
    _read_phase,
    _write_phase,
)

# A repo that was never wired has no phase file; free-flow still works there —
# the file is created at the entry phase so resume lands where a fresh wire
# would (intake runs on the first post-resume request).
_DEFAULT_PHASE = "intake"


def run_flow_free(root: Path | None = None) -> dict[str, Any]:
    """Enter free-flow: set ``mode: free`` + ``free_since`` in the phase file.

    Idempotent — already-free returns ``changed=False`` with the original
    ``free_since``. Never touches the ``phase`` value. Affects every session in
    the repo (the phase file is per-repo shared state).
    """
    from agentalloy.install.state import _repo_root  # pyright: ignore[reportPrivateUsage]

    root = root or _repo_root()
    data = _read_phase(root) or {}
    phase = data.get("phase") or _DEFAULT_PHASE
    if data.get("mode") == "free":
        return {
            "phase": phase,
            "mode": "free",
            "free_since": data.get("free_since"),
            "changed": False,
        }
    # Keep the phase line first (old parsers read line-by-line), preserve every
    # other existing key, then append the free-flow pair.
    new: dict[str, Any] = {"phase": phase}
    new.update({k: v for k, v in data.items() if k != "phase"})
    new["mode"] = "free"
    new["free_since"] = _now_iso()
    _write_phase(new, root)
    return {"phase": phase, "mode": "free", "free_since": new["free_since"], "changed": True}


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
    data = _read_phase(root) or {}
    phase = data.get("phase") or _DEFAULT_PHASE
    if data.get("mode") != "free":
        return {"phase": phase, "mode": "workflow", "changed": False}
    new: dict[str, Any] = {"phase": phase}
    new.update({k: v for k, v in data.items() if k not in ("phase", "mode", "free_since")})
    _write_phase(new, root)
    _clear_state(root, "free-reminded")
    return {"phase": phase, "mode": "workflow", "changed": True}


def run_flow_status(root: Path | None = None) -> dict[str, Any]:
    """Current flow mode, phase, and (when free) since-when."""
    from agentalloy.install.state import _repo_root  # pyright: ignore[reportPrivateUsage]

    root = root or _repo_root()
    data = _read_phase(root) or {}
    mode = "free" if data.get("mode") == "free" else "workflow"
    return {
        "phase": data.get("phase"),
        "mode": mode,
        "free_since": data.get("free_since") if mode == "free" else None,
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
            "Repo directory to read/write the .agentalloy/phase file in. "
            "Default: auto-detect from cwd (stops at $HOME)."
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
