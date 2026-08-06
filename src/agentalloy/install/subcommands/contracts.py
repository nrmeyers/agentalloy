# pyright: reportPrivateUsage=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
"""``contracts`` subcommand group — store-backed contract operations.

Operates on the whole contract collection (plural), as opposed to the singular
``contract`` group (validate/show/init a single file). All operations route
through ``StateClient`` over HTTP — service down means a non-zero exit naming
the service, never a silent local write.

Ships:

    agentalloy contracts archive [--phase <name>] [--slug <slug>] [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from agentalloy.api.state_client import StateClient, StateClientError
from agentalloy.install.output import add_json_flag, print_rich, write_result


def _get_client() -> StateClient:
    """Return a StateClient and verify the service is running."""
    client = StateClient()
    if not client.is_running():
        print(
            "Error: agentalloy service is not running. "
            "Start the service or run `agentalloy start`.",
            file=sys.stderr,
        )
        sys.exit(1)
    return client


def _run_archive(args: argparse.Namespace) -> int:
    """Archive active contracts through StateClient over HTTP.

    Lists active contracts (optionally filtered by phase/slug), then flips
    each to ``archived`` status via ``POST /contracts/{id}/archive``.
    Service down means a non-zero exit naming the service — never a silent
    local write.
    """
    client = _get_client()
    phase: str | None = getattr(args, "phase", None)
    slug: str | None = getattr(args, "slug", None)
    dry_run: bool = getattr(args, "dry_run", False)

    try:
        contracts = client.list_contracts(phase=phase, slug=slug, status="active")
    except StateClientError as exc:
        print(f"Error: {exc.message}", file=sys.stderr)
        return 1

    archived_ids: list[str] = []
    errors: list[str] = []

    if not dry_run:
        for contract in contracts:
            cid = contract.get("contract_id")
            if not cid:
                continue
            try:
                client.archive_contract(cid)
                archived_ids.append(cid)
            except StateClientError as exc:
                errors.append(f"{cid}: {exc.message}")

    payload: dict[str, Any] = {
        "dry_run": dry_run,
        "matched": len(contracts),
        "archived": len(archived_ids),
        "archived_ids": archived_ids,
        "errors": errors,
    }

    write_result(payload, args, human_fn=_render_archive)
    return 1 if errors else 0


def _render_archive(result: dict[str, Any]) -> None:
    dry_run = result.get("dry_run")
    matched = result.get("matched", 0)
    archived = result.get("archived", 0)
    archived_ids = result.get("archived_ids") or []
    errors = result.get("errors") or []

    print_rich("\n  [bold]Contracts archive[/bold]")
    if dry_run:
        print_rich("  [yellow]dry-run — nothing was archived[/yellow]")
        print_rich(f"  Would archive {matched} contract(s)")
    if matched == 0:
        print_rich("  [green]No active contracts matched — nothing to archive.[/green]\n")
        return
    if not dry_run and archived:
        print_rich(f"\n  [bold]Archived {archived}[/bold]")
        for cid in archived_ids:
            print_rich(f"  [green]→[/green] {cid}")
    if errors:
        print_rich(f"\n  [bold]Errors ({len(errors)})[/bold]")
        for err in errors:
            print_rich(f"  [red]![/red] {err}")
    print_rich()


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    p = subparsers.add_parser("contracts", help="Manage the contract collection (store-backed).")
    add_json_flag(p)
    sub = p.add_subparsers(dest="contracts_cmd")

    arch = sub.add_parser(
        "archive",
        help="Archive active contracts via the state service.",
    )
    add_json_flag(arch)
    arch.add_argument("--phase", default=None, help="Restrict to one phase (default: all).")
    arch.add_argument("--slug", default=None, help="Restrict to contracts whose slug matches.")
    arch.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would archive without flipping status.",
    )
    arch.set_defaults(func=_run_archive)

    def _show_help_and_exit(_a: object) -> int:
        p.print_help()
        return 0

    p.set_defaults(func=_show_help_and_exit)
