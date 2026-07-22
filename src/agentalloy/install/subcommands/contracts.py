"""``contracts`` subcommand group — tree-layout maintenance.

Operates on the whole ``.agentalloy/contracts/`` tree (plural), as opposed to
the singular ``contract`` group (validate/show/init a single file). Ships:

    agentalloy contracts migrate [--dry-run]

``migrate`` relocates a repo's legacy flat-layout contracts into the tree
(``active/<phase>/`` for live work-items, ``archive/<phase>/`` for completed /
superseded), placing each file by its own ``phase`` frontmatter. It is the
explicit form of the auto-migration the proxy runs on first read
(``skill_loader.ensure_migrated``); running it by hand gives a ``--dry-run``
preview and surfaces collisions / unreadable files the silent path only logs.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from agentalloy.install.output import add_json_flag, print_rich, write_result


def _plan_payload(project_root: Path) -> dict[str, Any]:
    from agentalloy.contracts import contracts_root, plan_contracts_migration

    root = contracts_root(project_root)
    plan = plan_contracts_migration(project_root)

    def rel(p: Path) -> str:
        try:
            return p.relative_to(root).as_posix()
        except ValueError:
            return str(p)

    return {
        "contracts_root": str(root),
        "moves": [
            {"from": rel(m.src), "to": rel(m.dst), "archived": m.archived} for m in plan.moves
        ],
        "collisions": [{"from": rel(s), "to": rel(d)} for s, d in plan.collisions],
        "unreadable": [rel(p) for p in plan.unreadable],
    }


def _run_migrate(args: argparse.Namespace) -> int:
    from agentalloy.contracts import (
        apply_contracts_migration,
        contracts_root,
        cursor_after_migration,
        plan_contracts_migration,
    )
    from agentalloy.install.state import _repo_root  # pyright: ignore[reportPrivateUsage]
    from agentalloy.signals.skill_loader import (  # pyright: ignore[reportPrivateUsage]
        _read_state,
        _write_state_atomic,
    )

    project_root = _repo_root()
    root = contracts_root(project_root)
    dry_run: bool = getattr(args, "dry_run", False)

    payload = _plan_payload(project_root)
    payload["dry_run"] = dry_run

    if not dry_run and payload["moves"]:
        plan = plan_contracts_migration(project_root)
        done = apply_contracts_migration(plan)
        payload["migrated"] = len(done)
        # Follow the moves: rewrite the shared cursor and every scoped cursor.
        names = ["cursor"]
        for f in (project_root / ".agentalloy").glob("cursor.*"):
            names.append(f.name)
        for name in names:
            val = _read_state(project_root, name)
            new = cursor_after_migration(val, done, root)
            if new is not None and new != val:
                _write_state_atomic(project_root, name, new)
    else:
        payload["migrated"] = 0

    write_result(payload, args, human_fn=_render_migrate)
    # Collisions are the only actionable failure — non-zero so scripts notice.
    return 1 if payload["collisions"] else 0


def _render_migrate(result: dict[str, Any]) -> None:
    moves = result.get("moves") or []
    collisions = result.get("collisions") or []
    unreadable = result.get("unreadable") or []
    dry_run = result.get("dry_run")

    print_rich("\n  [bold]Contracts migration[/bold]")
    print_rich(f"  Root: {result.get('contracts_root')}")
    if dry_run:
        print_rich("  [yellow]dry-run — nothing was moved[/yellow]")

    if not moves and not collisions and not unreadable:
        print_rich("  [green]Already on the tree layout — nothing to migrate.[/green]\n")
        return

    if moves:
        verb = "Would move" if dry_run else "Moved"
        print_rich(f"\n  [bold]{verb} {len(moves)}[/bold]")
        for m in moves:
            print_rich(f"  [green]→[/green] {m['from']}  ⇒  {m['to']}")
    if collisions:
        print_rich(f"\n  [bold]Collisions ({len(collisions)}) — left in place[/bold]")
        for c in collisions:
            print_rich(f"  [red]![/red] {c['from']}  ⇏  {c['to']} (destination occupied)")
        print_rich("  Resolve by renaming/removing the occupying file, then re-run.")
    if unreadable:
        print_rich(f"\n  [bold]Unreadable ({len(unreadable)}) — skipped[/bold]")
        for u in unreadable:
            print_rich(f"  [yellow]?[/yellow] {u} (no readable `phase` frontmatter)")
    print_rich()


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    p = subparsers.add_parser("contracts", help="Maintain the contracts tree layout.")
    add_json_flag(p)
    sub = p.add_subparsers(dest="contracts_cmd")

    mig = sub.add_parser(
        "migrate",
        help="Move legacy flat contracts into the active/<phase> + archive/<phase> tree.",
    )
    add_json_flag(mig)
    mig.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would move without touching any files.",
    )
    mig.set_defaults(func=_run_migrate)

    p.set_defaults(func=lambda _a: (p.print_help(), 0)[1])
