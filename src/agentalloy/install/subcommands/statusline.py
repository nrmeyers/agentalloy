# pyright: reportPrivateUsage=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
"""``statusline`` subcommand — Claude Code status-line renderer.

Claude Code invokes a configured ``statusLine.command`` once per turn, piping a
JSON session object on stdin and rendering the command's first stdout line in
the status bar. This command reads the phase via ``StateClient`` over HTTP and
prints a compact ``agentalloy ▸ <phase>`` line, so the active SDD phase is
*standing state* — visible every turn without the proxy injecting anything.

When the service is down, renders a ``[degraded]`` badge instead of a stale or
invented phase. A status glyph is a display surface, not a state mutation, so
the fail-loud rule does not apply — but the badge must never show stale data.

It is wired into ``.claude/settings.json`` by ``wire`` (full mode). It must be
fast and must never fail loudly: any error prints nothing (an empty status line)
rather than a traceback into the status bar.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

# Shown when a phase is active. Kept ASCII-plus-one-glyph so it renders in any
# terminal Claude Code runs in.
_PREFIX = "⚙ agentalloy"


def _cwd_from_stdin() -> Path | None:
    """Project dir from the Claude Code status-line JSON on stdin, or None.

    Claude Code pipes ``{"workspace": {"current_dir": ...}, "cwd": ...}``. We
    read it only when stdin is not a TTY (i.e. something is actually piped) so an
    interactive ``agentalloy statusline`` invocation doesn't block on input.
    """
    if sys.stdin.isatty():
        return None
    try:
        raw = sys.stdin.read()
    except OSError:
        return None
    if not raw.strip():
        return None
    try:
        data: Any = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    workspace = data.get("workspace")
    if isinstance(workspace, dict):
        cur = workspace.get("current_dir")
        if isinstance(cur, str) and cur:
            return Path(cur)
    cwd = data.get("cwd")
    if isinstance(cwd, str) and cwd:
        return Path(cwd)
    return None


def _find_repo_root(start: Path) -> Path | None:
    """Walk up from *start* for a ``.agentalloy/config`` file, returning the
    repo root or None.

    Used to determine whether we're inside a wired repo. The config file
    persists across the full lifecycle (unlike phase, which is store-only now).
    """
    home = Path.home().resolve()
    try:
        cur = start.resolve()
    except OSError:
        return None
    seen = 0
    while True:
        config_file = cur / ".agentalloy" / "config"
        if config_file.is_file():
            return cur
        if cur == home or cur.parent == cur or seen > 64:
            return None
        cur = cur.parent
        seen += 1


def _get_phase_from_service() -> tuple[str | None, bool]:
    """Read the current phase from the state service via StateClient.

    Returns ``(phase, service_running)``. When the service is down, phase is
    None and service_running is False — the caller renders a degraded badge
    instead of a stale value.
    """
    try:
        from agentalloy.api.state_client import (
            StateClient,  # noqa: PLC0415 — keep import off cold paths
        )

        client = StateClient()
        if not client.is_running():
            return None, False
        raw = client.get_state("phase")
        return raw, True
    except Exception:
        return None, False


def _release_badge() -> str:
    """A compact ``  ↑<version>`` suffix when a newer release is available, else "".

    Cache-only (never touches the network) and fully fail-silent so it can never
    break the per-turn status line. The producer is the running service.
    """
    try:
        from agentalloy.install import release_check  # noqa: PLC0415 — keep import off cold paths

        info = release_check.notice()
        if not info:
            return ""
        return f"  ↑{info['latest'].lstrip('v')}"
    except Exception:
        return ""


def render_statusline(root: Path | None) -> str:
    """The status-line string for *root* (cwd when None), or "" when inactive.

    Reads phase via StateClient over HTTP. When the service is down and we're
    inside a wired repo, renders a ``[degraded]`` badge — never a stale or
    invented phase. Outside a wired repo, returns "".
    """
    start = root or _cwd_from_stdin() or Path(os.getcwd())

    # Check if we're inside a wired repo
    repo_root = _find_repo_root(start)
    if repo_root is None:
        return ""

    phase, service_running = _get_phase_from_service()

    if not service_running:
        return f"{_PREFIX} ▸ [degraded]"

    if not phase:
        return ""

    return f"{_PREFIX} ▸ {phase}{_release_badge()}"


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    p: argparse.ArgumentParser = subparsers.add_parser(
        "statusline",
        help="Render the Claude Code status line for the current repo's phase.",
    )
    p.add_argument(
        "--project-root",
        default=None,
        help="Repo directory to read the phase from. Default: stdin JSON, then cwd.",
    )
    p.set_defaults(func=_run)


def _run(args: argparse.Namespace) -> int:
    # Never fail into the status bar: any unexpected error prints an empty line.
    try:
        pr = getattr(args, "project_root", None)
        root = Path(pr).expanduser().resolve() if pr else None
        line = render_statusline(root)
    except Exception:
        line = ""
    print(line)
    return 0
