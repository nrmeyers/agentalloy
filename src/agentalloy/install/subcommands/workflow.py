# pyright: reportPrivateUsage=false
"""``workflow`` subcommand — workflow pause mode management.

Workflow pause is a per-repo mode for sessions with no specific task in mind: it
pauses ALL workflow steering (orientation, banners, exit gates, phase
transitions, intake) while keeping domain-skill composition. It rides the same
per-repo ``phase`` row the phase machine uses, as an optional ``mode: paused`` +
``paused_since: <iso>`` pair — entering pause never changes the ``phase``
value, so resume returns to exactly the prior phase.

Like ``phase set``, these are deterministic per-repo writes (no LM involvement)
and the phase row is SHARED by every concurrent session in the repo:
``workflow pause`` / ``workflow resume`` affect all of them, not just yours.

Commands:
    agentalloy workflow pause    — pause workflow steering (idempotent)
    agentalloy workflow resume  — resume workflow at the prior phase (idempotent)
    agentalloy workflow status  — current mode, phase, and since-when
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

from agentalloy.api.state_client import StateClientError
from agentalloy.install.subcommands._state import fail_on_state_error, phase_access
from agentalloy.install.subcommands.phase import _now_iso

logger = logging.getLogger(__name__)

# A repo that was never wired has no phase row; pause still works there —
# the row is created at the entry phase so resume lands where a fresh wire
# would (intake runs on the first post-resume request).
_DEFAULT_PHASE = "intake"


def _rewrite_and_verify_posture(root: Path, phase: str, mode: str) -> None:
    """Rewrite the Tier A enforcement posture for ``(phase, mode)`` and confirm it stuck.

    Called from the CLI process, where ``mode`` is already known (the caller
    just wrote it) — passed explicitly rather than re-derived, since the CLI
    cannot reach the in-process store to re-read it reliably (that is exactly
    the bug this function exists to not repeat).

    Non-fatal by design: a posture failure must not block ``workflow pause``/``workflow
    resume`` from completing the mode write, which already succeeded by the
    time this runs. But it must not be silent either — a warning to stderr
    (and the log) replaces the old bare ``except Exception: pass``, which
    turned total failure of the command's purpose into nothing happening.
    """
    from agentalloy.install.subcommands.wire_harness import (
        rewrite_enforcement_posture,
        verify_enforcement_posture,
    )

    try:
        rewrite_enforcement_posture(root, phase, mode=mode)
    except Exception:
        logger.warning(
            "posture rewrite failed for %s phase=%s mode=%s",
            root,
            phase,
            mode,
            exc_info=True,
        )
        print(
            f"Warning: could not update enforcement posture for phase '{phase}' "
            f"(mode '{mode}') — write gates may be stale. Run `agentalloy workflow status` "
            "and check `.claude/settings.local.json` / `.codex/config.toml` by hand.",
            file=sys.stderr,
        )
        return

    try:
        mismatched = verify_enforcement_posture(root, phase, mode)
    except Exception:
        logger.warning(
            "posture verification failed for %s phase=%s mode=%s",
            root,
            phase,
            mode,
            exc_info=True,
        )
        return

    if mismatched:
        logger.warning(
            "posture mismatch after rewrite for %s phase=%s mode=%s: %s",
            root,
            phase,
            mode,
            mismatched,
        )
        print(
            f"Warning: enforcement posture for {', '.join(mismatched)} did not update to "
            f"match phase '{phase}' (mode '{mode}') — write gates may be stale.",
            file=sys.stderr,
        )


def run_workflow_pause(root: Path | None = None) -> dict[str, Any]:
    """Enter workflow pause: set ``mode: paused`` + ``paused_since`` on the phase row.

    Idempotent — already-paused returns ``changed=False`` with the original
    ``paused_since``. Never touches the ``phase`` value. Affects every session in
    the repo (the phase row is per-repo shared state).

    ``mode`` and ``paused_since`` are written as their own fields.  They used to
    be smuggled into the phase *name* (``"free-flow:design"``), which stored a
    phase no consumer recognises and forced the call to skip the posture
    rewrite so the bogus name would not clear the deny rules.
    """
    from agentalloy.install.state import _repo_root

    root = root or _repo_root()
    access = phase_access(root)
    try:
        state = access.read()
        phase = state.phase if state else _DEFAULT_PHASE
        if state is not None and (state.mode or "").lower() in ("paused", "free"):
            # Legacy alias: ``mode: free`` reads as paused.
            resolved_mode = "paused" if (state.mode or "").lower() != "free" else "free"
            return {
                "phase": phase,
                "mode": resolved_mode,
                "paused_since": state.paused_since,
                "changed": False,
            }
        since = _now_iso()
        access.write(phase, mode="paused", paused_since=since)
        # Immediately update Tier A harness configs so pause writes take effect.
        # Mode is passed explicitly (known here, just written) rather than
        # re-derived — the CLI process cannot reach the in-process store to
        # re-read it reliably. See `_rewrite_and_verify_posture`.
        _rewrite_and_verify_posture(root, phase, "paused")
    except StateClientError as exc:
        fail_on_state_error(exc)
        raise  # unreachable
    return {"phase": phase, "mode": "paused", "paused_since": since, "changed": True}


def run_workflow_resume(root: Path | None = None) -> dict[str, Any]:
    """Leave workflow pause: clear ``mode``/``paused_since``, restoring the exact prior
    phase. Idempotent — a repo not in pause returns ``changed=False``.

    The announced marker is deliberately left alone: while paused, it holds the
    pause sentinel, which mismatches every real phase — so the next proxy request
    re-orients (intake included) as a first request. Affects every session in the
    repo.

    Re-engages the enforcement posture for the restored phase — this is the
    dangerous polarity: if the rewrite were inert here the way it used to be
    on the ``paused`` side, gates would fail to RE-ENGAGE on resume, silently
    leaving writes open after the escape hatch closes.
    """
    from agentalloy.install.state import _repo_root

    root = root or _repo_root()
    access = phase_access(root)
    try:
        state = access.read()
        phase = state.phase if state else _DEFAULT_PHASE
        if state is None or (state.mode or "").lower() not in ("paused", "free"):
            return {"phase": phase, "mode": "workflow", "changed": False}
        # Empty strings *clear* the pair; None would carry it forward.
        access.write(phase, mode="", paused_since="")
        _rewrite_and_verify_posture(root, phase, "workflow")
    except StateClientError as exc:
        fail_on_state_error(exc)
        raise  # unreachable
    return {"phase": phase, "mode": "workflow", "changed": True}


def run_workflow_status(root: Path | None = None) -> dict[str, Any]:
    """Current workflow mode, phase, and (when paused) since-when."""
    from agentalloy.install.state import _repo_root

    root = root or _repo_root()
    try:
        state = phase_access(root).read()
    except StateClientError as exc:
        fail_on_state_error(exc)
        raise  # unreachable
    mode = (
        "paused"
        if state is not None and (state.mode or "").lower() in ("paused", "free")
        else "workflow"
    )
    return {
        "phase": state.phase if state else None,
        "mode": mode,
        "paused_since": (state.paused_since if state else None) if mode != "workflow" else None,
    }


# ---------------------------------------------------------------------------
# Subcommand interface
# ---------------------------------------------------------------------------


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    p: argparse.ArgumentParser = subparsers.add_parser(
        "workflow",
        help="Workflow pause mode: pause/resume workflow steering (pause, resume, status).",
    )
    sub = p.add_subparsers(dest="workflow_action")

    p_pause = sub.add_parser("pause", help="Pause workflow steering; keep skill composition")
    _add_project_root_flag(p_pause)
    p_pause.set_defaults(func=_run_pause)

    p_resume = sub.add_parser("resume", help="Resume the workflow at the prior phase")
    _add_project_root_flag(p_resume)
    p_resume.set_defaults(func=_run_resume)

    p_status = sub.add_parser("status", help="Show the current workflow mode")
    _add_project_root_flag(p_status)
    p_status.set_defaults(func=_run_status)

    # approve (alias — delegates to the top-level approve command)
    from agentalloy.install.subcommands.approve import add_subparser as _add_approve_subparser

    _add_approve_subparser(sub)

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


def _run_pause(args: argparse.Namespace) -> int:
    result = run_workflow_pause(root=_resolve_root(args))
    if result["changed"]:
        print(f"Workflow pause enabled — workflow paused at phase '{result['phase']}'.")
        print("Domain skills still compose. Run `agentalloy workflow resume` when ready.")
    else:
        print(
            f"Already in workflow pause (since {result.get('paused_since') or 'unknown'}); "
            f"workflow paused at phase '{result['phase']}'.",
        )
    return 0


def _run_resume(args: argparse.Namespace) -> int:
    result = run_workflow_resume(root=_resolve_root(args))
    if result["changed"]:
        print(f"Resuming workflow at phase '{result['phase']}'.")
    else:
        print(f"Not in workflow pause; workflow already active at phase '{result['phase']}'.")
    return 0


def _run_status(args: argparse.Namespace) -> int:
    result = run_workflow_status(root=_resolve_root(args))
    print(f"Mode: {result['mode']}")
    print(f"Phase: {result['phase'] or 'none'}")
    if result.get("paused_since"):
        print(f"Paused since: {result['paused_since']}")
    return 0
