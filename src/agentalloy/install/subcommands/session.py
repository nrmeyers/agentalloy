"""Session management CLI — list, stash, resume, archive, cancel sessions.

Part of WI-2 (multi-session-management): persistent session registry with
orientation flow for new sessions.

Lifecycle verbs follow the four-state model: ``stash`` parks a session
(waiting to resume), ``resume`` brings it back, ``archive`` closes it as
reached-product, ``cancel`` closes it as abandoned.
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


def run_session_stash(args: argparse.Namespace) -> int:
    """Stash a session by session_key (park it, waiting to resume)."""
    session_key = args.session_key
    access = phase_access(Path.cwd(), autostart=False)
    stashed = access.session_handle().stash_session(session_key)
    if stashed:
        print(f"Stashed session: {session_key}")
        return 0
    else:
        print(f"Session not found or not active: {session_key}", file=sys.stderr)
        return 1


def run_session_cancel(args: argparse.Namespace) -> int:
    """Cancel a session by session_key (abandoned, never reached product)."""
    session_key = args.session_key
    access = phase_access(Path.cwd(), autostart=False)
    cancelled = access.session_handle().cancel_session(session_key)
    if cancelled:
        print(f"Cancelled session: {session_key}")
        return 0
    else:
        print(f"Session not found or not active: {session_key}", file=sys.stderr)
        return 1


def run_session_archive(args: argparse.Namespace) -> int:
    """Archive a session by session_key (work item reached product)."""
    session_key = args.session_key
    access = phase_access(Path.cwd(), autostart=False)
    archived = access.session_handle().archive_session(session_key)
    if archived:
        print(f"Archived session: {session_key}")
        return 0
    else:
        print(f"Session not found or not active: {session_key}", file=sys.stderr)
        return 1


def run_session_resume(args: argparse.Namespace) -> int:
    """Re-activate a session by session_key (stashed → active)."""
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
        help="Session management (list, stash, resume, archive, cancel)",
    )
    session_subparsers = session_parser.add_subparsers(dest="session_verb", required=True)

    # session list
    list_parser = session_subparsers.add_parser("list", help="List active sessions")
    list_parser.set_defaults(func=run_session_list)

    # session stash <session_key>
    stash_parser = session_subparsers.add_parser(
        "stash", help="Stash a session (park it, waiting to resume)"
    )
    stash_parser.add_argument("session_key", help="Session key to stash")
    stash_parser.set_defaults(func=run_session_stash)

    # session resume <session_key>
    resume_parser = session_subparsers.add_parser(
        "resume", help="Re-activate a stashed session (stashed → active)"
    )
    resume_parser.add_argument("session_key", help="Session key to resume")
    resume_parser.set_defaults(func=run_session_resume)

    # session archive <session_key>
    archive_parser = session_subparsers.add_parser(
        "archive", help="Archive a session (work item reached product)"
    )
    archive_parser.add_argument("session_key", help="Session key to archive")
    archive_parser.set_defaults(func=run_session_archive)

    # session cancel <session_key>
    cancel_parser = session_subparsers.add_parser(
        "cancel", help="Cancel a session (abandoned, never reached product)"
    )
    cancel_parser.add_argument("session_key", help="Session key to cancel")
    cancel_parser.set_defaults(func=run_session_cancel)
