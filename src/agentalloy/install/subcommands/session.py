"""Session management CLI — list, resume, archive sessions.

Part of WI-2 (multi-session-management): persistent session registry with
orientation flow for new sessions.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agentalloy.install.subcommands._state import phase_access


def run_session_list(args: argparse.Namespace) -> int:
    """List active sessions for the current repo+stream."""
    access = phase_access(Path.cwd(), autostart=False)
    sessions = access.session_handle().list_active_sessions()
    if not sessions:
        print("No active sessions.")
        return 0
    print(f"Active sessions ({len(sessions)}):")
    for s in sessions:
        task = s["task_slug"] or "(no task)"
        phase = s["phase"] or "(no phase)"
        last_active = s["last_active_at"]
        print(f"  {s['session_key']}: {task} @ {phase} (last active: {last_active})")
    return 0


def run_session_archive(args: argparse.Namespace) -> int:
    """Archive a session by session_key."""
    session_key = args.session_key
    access = phase_access(Path.cwd(), autostart=False)
    archived = access.session_handle().archive_session(session_key)
    if archived:
        print(f"Archived session: {session_key}")
        return 0
    else:
        print(f"Session not found or already archived: {session_key}", file=sys.stderr)
        return 1


def run_session_resume(args: argparse.Namespace) -> int:
    """Re-activate a session by session_key (archived → active)."""
    session_key = args.session_key
    access = phase_access(Path.cwd(), autostart=False)
    resumed = access.session_handle().resume_session(session_key)
    if resumed:
        print(f"Resumed session: {session_key}")
        return 0
    else:
        print(f"Session not found: {session_key}", file=sys.stderr)
        return 1


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the 'session' subcommand and its verbs."""
    session_parser = subparsers.add_parser(
        "session",
        help="Session management (list, resume, archive)",
    )
    session_subparsers = session_parser.add_subparsers(dest="session_verb", required=True)

    # session list
    list_parser = session_subparsers.add_parser("list", help="List active sessions")
    list_parser.set_defaults(func=run_session_list)

    # session resume <session_key>
    resume_parser = session_subparsers.add_parser(
        "resume", help="Re-activate a session (archived → active)"
    )
    resume_parser.add_argument("session_key", help="Session key to resume")
    resume_parser.set_defaults(func=run_session_resume)

    # session archive <session_key>
    archive_parser = session_subparsers.add_parser("archive", help="Archive a session")
    archive_parser.add_argument("session_key", help="Session key to archive")
    archive_parser.set_defaults(func=run_session_archive)
