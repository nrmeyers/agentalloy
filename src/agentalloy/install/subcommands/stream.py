# pyright: reportPrivateUsage=false
"""``stream`` subcommand — manage per-worktree stream_id for issue #548.

When a repository has multiple git worktrees, each worktree needs its own
workflow state (phase, contracts, approvals) in the DuckDB store. The
``stream_id`` column provides that isolation while ``repo_slug`` keeps
code-index lookups worktree-independent.

Commands:
    agentalloy stream              — print the resolved stream_id
    agentalloy stream use <id>     — pin an explicit stream_id for this worktree
    agentalloy stream clear        — remove the pin (falls back to path hash)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from agentalloy.install.state import _repo_root
from agentalloy.storage.stream_id import bind_stream_id, resolve_stream_id


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    p: argparse.ArgumentParser = subparsers.add_parser(
        "stream",
        help="Manage per-worktree stream_id for workflow state isolation.",
    )
    sub = p.add_subparsers(dest="stream_action")

    # ``stream use <id>``
    p_use = sub.add_parser("use", help="Pin an explicit stream_id for this worktree")
    p_use.add_argument(
        "stream_id",
        help="Stream identifier to bind (any non-empty string).",
    )
    _add_project_root_flag(p_use)
    p_use.set_defaults(func=_run_use)

    # ``stream clear``
    p_clear = sub.add_parser(
        "clear",
        help="Remove the pinned stream_id (falls back to worktree path hash)",
    )
    _add_project_root_flag(p_clear)
    p_clear.set_defaults(func=_run_clear)

    # ``stream status``
    p_status = sub.add_parser(
        "status",
        help="Show the current stream_id and how it was resolved",
    )
    _add_project_root_flag(p_status)
    p_status.set_defaults(func=_run_status)

    # Default action (no subcommand) = status
    p.set_defaults(func=_run_status)


def _add_project_root_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--project-root",
        default=None,
        help=("Worktree root to act on. Default: auto-detect from cwd (stops at $HOME)."),
    )


def _resolve_root(args: argparse.Namespace) -> Path:
    pr = getattr(args, "project_root", None)
    if pr:
        return Path(pr).expanduser().resolve()
    return _repo_root()


def _run_use(args: argparse.Namespace) -> int:
    root = _resolve_root(args)
    if not root.exists():
        print(f"Error: project root does not exist: {root}", file=sys.stderr)
        return 1
    stream_id: str = args.stream_id.strip()
    if not stream_id:
        print("Error: stream_id must be a non-empty string.", file=sys.stderr)
        return 1

    bind_stream_id(root, stream_id)
    print(f"Bound stream_id '{stream_id}' for {root}")
    return 0


def _run_clear(args: argparse.Namespace) -> int:
    root = _resolve_root(args)
    stream_file = root / ".agentalloy" / ".stream"

    if not stream_file.is_file():
        print(f"No pinned stream_id at {root} — already using path hash.")
        return 0

    stream_file.unlink(missing_ok=True)
    # Clean up the .agentalloy dir only if it's now empty (non-recursive).
    agentalloy_dir = root / ".agentalloy"
    try:
        if agentalloy_dir.is_dir() and not any(agentalloy_dir.iterdir()):
            agentalloy_dir.rmdir()
    except OSError:
        pass  # non-empty or race — leave it

    print(f"Cleared pinned stream_id for {root} — will fall back to worktree path hash.")
    return 0


def _run_status(args: argparse.Namespace) -> int:
    root = _resolve_root(args)
    stream_file = root / ".agentalloy" / ".stream"
    resolved = resolve_stream_id(root)

    # Determine the source of the resolved value.
    env_val = os.environ.get("AGENTALLOY_STREAM_ID", "").strip()
    if env_val:
        source = "AGENTALLOY_STREAM_ID env var"
    elif stream_file.is_file():
        source = ".agentalloy/.stream"
    else:
        source = "worktree path hash"

    print(f"stream_id: {resolved}")
    print(f"source:    {source}")
    print(f"root:      {root}")
    return 0
