# pyright: reportPrivateUsage=false
"""``task`` subcommand — per-work-item cursor for Tier 2 domain injection.

The build phase fans a phase out into many per-task contracts under
``.agentalloy/contracts/<phase>/``. The proxy injects a task's domain skills the
turn after the cursor moves to it (see ``proxy_signal._resolve_current_contract``
and the ``composed`` cadence). This subcommand moves that cursor:

    agentalloy task next          — advance the cursor to the next contract
    agentalloy task start <slug>  — point the cursor at a named contract
    agentalloy task status        — show the cursor and the ordered work-list

The cursor value is a contracts-relative posix path (e.g. ``build/01-cache.md``).
Contracts are ordered by filename, so design controls the worklist order by
prefixing (``01-``, ``02-``). Emitting a single ``task next`` per task is the one
mechanical signal the build LLM gives — no skill-selection reasoning; the proxy
front-loads the matched skills + the task before the LLM writes code.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, cast

from agentalloy.api.state_client import StateClient, StateClientError
from agentalloy.install.subcommands._state import fail_on_state_error, phase_access
from agentalloy.signals.skill_loader import (  # type: ignore[reportPrivateUsage]
    _read_cursor,
    _write_cursor_atomic,
    cli_session_key,
)


def _active_phase(root: Path) -> str | None:
    """The repo's current phase, read from the store.

    ``None`` means the repo genuinely has no phase.  An unreachable store exits
    non-zero rather than returning ``None``: "no active phase" and "the service
    is down" used to be the same answer here, so an outage silently reported
    itself as a repo with no work to do.
    """
    try:
        state = phase_access(root).read()
    except StateClientError as exc:
        fail_on_state_error(exc)
        raise  # unreachable
    return state.phase if state else None


def _store_cursor(cid: str, root: Path) -> None:
    """Record the cursor — through the service when it is up, else in-process.

    The cursor lives in the state store, and the store handle belongs to the
    service process (DuckDB is single-writer).  An out-of-process CLI routes
    the write over HTTP; in-process (the service itself, or the test suite)
    nothing is up to loop back to, so the bound store is written directly.
    """
    client = StateClient()
    if client.is_running():
        try:
            client.set_cursor(cid)
            return
        except StateClientError as exc:
            fail_on_state_error(exc)
    _write_cursor_atomic(root, cid, cli_session_key())


def _current_cursor(root: Path) -> str | None:
    """The cursor the proxy would resolve for this CLI session right now.

    Mirrors the proxy's resolution order — the session-scoped row first, then
    the shared cursor (``_resolve_current_contract``) — so ``task next``
    advances from exactly where the proxy is composing.

    The CLI process holds no state store (only the service binds one), so the
    out-of-process read goes over HTTP — the same transport the cursor write
    uses.  An in-process-only read returns ``None`` in the CLI, and treating
    that as "no cursor" is exactly how ``task next`` reset to task 1 on every
    call.  Callers reach this after ``_active_phase`` has established a bound
    store or a running service, so finding neither is a real failure, not an
    empty cursor.
    """
    from agentalloy.storage.state_store import process_store

    session_key = cli_session_key()
    if process_store() is not None:
        # In-process (service/tests): the bound store is the truth — an HTTP
        # loopback would be the service calling itself.
        cursor = _read_cursor(root, session_key) if session_key else None
        if not cursor:
            cursor = _read_cursor(root, None)
        return cursor

    client = StateClient()
    if not client.is_running():
        fail_on_state_error(StateClientError("cursor read: agentalloy service is not running"))
    cursor = client.get_scoped_cursor(session_key) if session_key else None
    if not cursor:
        cursor = client.get_state("cursor")
    return cursor.strip() if cursor else None


def _ordered_contracts(root: Path, phase: str) -> list[dict[str, Any]]:
    """All active contracts for *phase*, ordered by filename (contract_id).

    Delegates to the state store.  Returns a list of dicts with at least
    ``contract_id`` — a contracts-relative path that already carries the
    phase subdirectory (e.g. ``build/01-cache``), so it is not the bare
    filename stem.
    """
    access = phase_access(root)
    try:
        rows = access.contracts_handle().list_contracts(phase=phase, status="active")
    except StateClientError as exc:
        fail_on_state_error(exc)
        raise  # unreachable
    # Sort by contract_id to preserve filename ordering (01-foo, 02-bar, …).
    return sorted(rows, key=lambda r: str(r.get("contract_id", "")))


def _cursor_id(phase: str, contract: dict[str, Any]) -> str:
    """Contracts-relative posix id stored in ``.agentalloy/cursor``.

    Carries the ``active/`` prefix so it resolves against the tree layout
    (``.agentalloy/contracts/active/<phase>/``) — the single format shared with
    ``first_workitem_id`` and ``resolve_current_contract``.
    """
    return f"active/{phase}/{contract['contract_id']}.md"


def run_task_next(root: Path) -> dict[str, object]:
    """Advance the cursor to the next contract after the current one."""
    phase = _active_phase(root)
    if phase is None:
        return {"ok": False, "message": "No active phase."}
    contracts = _ordered_contracts(root, phase)
    if not contracts:
        return {"ok": False, "message": f"No contracts under .agentalloy/contracts/{phase}/."}

    # The cursor is stored as a full ``_cursor_id`` (``active/<phase>/<id>.md``),
    # so compare it against the same ids the worklist is built from. Matching on
    # the bare filename would never find the current task (contract_id is a
    # phase-prefixed path) and reset the cursor to task 1 on every call.
    ids = [_cursor_id(phase, c) for c in contracts]
    cursor = _current_cursor(root)
    # No/unknown cursor → start at the first task.
    nxt = ids.index(cursor) + 1 if cursor in ids else 0

    if nxt >= len(contracts):
        return {"ok": True, "done": True, "message": f"All {len(contracts)} tasks composed."}

    cid = _cursor_id(phase, contracts[nxt])
    _store_cursor(cid, root)
    return {"ok": True, "cursor": cid, "index": nxt + 1, "total": len(contracts)}


def run_task_start(slug: str, root: Path) -> dict[str, object]:
    """Point the cursor at the contract whose filename stem (or name) matches *slug*."""
    phase = _active_phase(root)
    if phase is None:
        return {"ok": False, "message": "No active phase."}
    contracts = _ordered_contracts(root, phase)
    for c in contracts:
        cid_val = c["contract_id"]
        # Contract ids are phase-prefixed paths (build/01-cache); the slug may
        # be the bare filename stem (01-cache) or either form with ``.md``.
        stem = cid_val.rsplit("/", 1)[-1]
        if slug in (cid_val, stem, f"{cid_val}.md", f"{stem}.md"):
            cid = _cursor_id(phase, c)
            _store_cursor(cid, root)
            return {"ok": True, "cursor": cid}
    return {"ok": False, "message": f"No contract matching '{slug}' under contracts/{phase}/."}


def run_task_status(root: Path) -> dict[str, object]:
    """Report the current cursor and the ordered worklist for the active phase.

    Always reports phase *and* worklist.  The service-up path used to return
    ``{"ok": True, "cursor": ...}`` alone, so `task status` rendered an empty
    worklist whenever the service happened to be running — the one case where
    the command had the most to say.
    """
    phase = _active_phase(root)
    if phase is None:
        return {"ok": False, "message": "No active phase."}

    # Same read as ``task next`` — the two surfaces must agree on what the
    # cursor currently points at, or ``status`` lies about what ``next`` will
    # do.
    cursor: str | None = _current_cursor(root)

    contracts = _ordered_contracts(root, phase)
    return {
        "ok": True,
        "phase": phase,
        "cursor": cursor,
        "worklist": [_cursor_id(phase, c) for c in contracts],
    }


# ---------------------------------------------------------------------------
# Subcommand interface
# ---------------------------------------------------------------------------


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    p: argparse.ArgumentParser = subparsers.add_parser(
        "task",
        help="Advance the per-work-item cursor that drives Tier 2 domain injection.",
    )
    _add_project_root_flag(p)
    sub = p.add_subparsers(dest="task_action")

    p_next = sub.add_parser("next", help="Advance the cursor to the next contract")
    _add_project_root_flag(p_next)
    p_next.set_defaults(func=_run_next)

    p_start = sub.add_parser("start", help="Point the cursor at a named contract")
    p_start.add_argument("slug", help="Contract filename stem (or full name) in the current phase")
    _add_project_root_flag(p_start)
    p_start.set_defaults(func=_run_start)

    p_status = sub.add_parser("status", help="Show the cursor and ordered worklist")
    _add_project_root_flag(p_status)
    p_status.set_defaults(func=_run_status)

    # Default action (no subcommand) = status
    p.set_defaults(func=_run_status)


def _add_project_root_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--project-root",
        default=None,
        help="Repo directory holding .agentalloy/. Default: auto-detect from cwd (stops at $HOME).",
    )


def _resolve_root(args: argparse.Namespace) -> Path:
    pr = getattr(args, "project_root", None)
    if pr:
        return Path(pr).expanduser().resolve()
    from agentalloy.install.state import _repo_root

    return _repo_root()


def _run_next(args: argparse.Namespace) -> int:
    result = run_task_next(_resolve_root(args))
    if not result.get("ok"):
        print(result.get("message", "task next failed"), file=sys.stderr)
        return 1
    if result.get("done"):
        print(result["message"])
        return 0
    print(f"Task {result['index']}/{result['total']} → {result['cursor']}")
    return 0


def _run_start(args: argparse.Namespace) -> int:
    result = run_task_start(args.slug, _resolve_root(args))
    if not result.get("ok"):
        print(result.get("message", "task start failed"), file=sys.stderr)
        return 1
    print(f"Cursor → {result['cursor']}")
    return 0


def _run_status(args: argparse.Namespace) -> int:
    result = run_task_status(_resolve_root(args))
    if not result.get("ok"):
        print(result.get("message", "no status"), file=sys.stderr)
        return 1
    print(f"Phase: {result['phase']}")
    print(f"Cursor: {result['cursor'] or '(none — phase default)'}")
    worklist = cast("list[str]", result.get("worklist") or [])
    if worklist:
        print("Worklist:")
        for item in worklist:
            marker = "→" if item == result["cursor"] else " "
            print(f"  {marker} {item}")
    return 0
