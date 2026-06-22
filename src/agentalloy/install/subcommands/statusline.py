"""``statusline`` subcommand — Claude Code status-line renderer.

Claude Code invokes a configured ``statusLine.command`` once per turn, piping a
JSON session object on stdin and rendering the command's first stdout line in
the status bar. This command reads the repo's ``.agentalloy/phase`` and prints a
compact ``agentalloy ▸ <phase>`` line, so the active SDD phase is *standing
state* — visible every turn without the proxy injecting anything.

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


def _find_phase(start: Path) -> str | None:
    """Walk up from *start* (stopping at $HOME / filesystem root) for a phase.

    Claude Code usually runs the status-line command at the workspace root, but
    a subdirectory cwd should still resolve the repo's phase. Mirrors the
    ``.agentalloy/phase`` location used elsewhere; reads the flat ``phase: <v>``
    line directly to avoid importing the YAML path on a hot per-turn command.
    """
    home = Path.home().resolve()
    try:
        cur = start.resolve()
    except OSError:
        return None
    seen = 0
    while True:
        phase_file = cur / ".agentalloy" / "phase"
        if phase_file.is_file():
            try:
                for line in phase_file.read_text(encoding="utf-8").splitlines():
                    if ":" not in line or line.strip().startswith("#"):
                        continue
                    key, _, value = line.partition(":")
                    if key.strip() == "phase":
                        return value.strip().strip('"').strip("'") or None
            except OSError:
                return None
            return None
        if cur == home or cur.parent == cur or seen > 64:
            return None
        cur = cur.parent
        seen += 1


def render_statusline(root: Path | None) -> str:
    """The status-line string for *root* (cwd when None), or "" when inactive."""
    start = root or _cwd_from_stdin() or Path(os.getcwd())
    phase = _find_phase(start)
    if not phase:
        return ""
    return f"{_PREFIX} ▸ {phase}"


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
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
