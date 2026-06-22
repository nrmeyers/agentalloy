"""``cleanup`` subcommand — recover from orphaned runtime artifacts.

The user-facing recovery verb. A native install leaves three classes of
artifact on the host (see :mod:`agentalloy.install.runtime_artifacts`):

1. **Processes** — our own ``uvicorn`` / ``llama-server`` runners squatting a
   runtime port (47950/47951/47952) with no live supervisor.
2. **Service units** — stale systemd user units / launchd LaunchAgents.
3. **The llama-server shim** — a dangling ``~/.local/bin/llama-server``
   launcher pointing at a prebuilt that no longer exists.

``cleanup`` reaps all three in one pass via
:func:`runtime_artifacts.reap`. It is foreign-safe: a *foreign* process
holding one of our ports is reported (``warn_foreign``) but never killed, and
a user's own ``llama-server`` on PATH is never removed.

Behavior
--------
- ``--dry-run`` prints the planned actions (from ``reap("all", dry_run=True)``)
  plus any foreign-holder conflicts from ``detect_orphans()`` as advisory
  notes, and mutates nothing.
- Default prints the same plan, then prompts ``Proceed? [y/N]`` (skipped with
  ``--yes``); on confirm it performs the reap and reports each executed action,
  surfacing any ``warn_foreign`` advisories as warnings.
- ``--json`` emits a structured result instead of human text.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from agentalloy.install import runtime_artifacts
from agentalloy.install.output import add_json_flag, print_rich, write_result

SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Subcommand interface
# ---------------------------------------------------------------------------


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    p: argparse.ArgumentParser = subparsers.add_parser(
        "cleanup",
        help="Reap orphaned llama-server processes, stale service units, and a dangling shim.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print the plan without mutating anything.",
    )
    p.add_argument(
        "--yes",
        "-y",
        action="store_true",
        default=False,
        help="Skip the confirmation prompt and reap immediately.",
    )
    add_json_flag(p)
    p.set_defaults(func=_run)


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _action_dict(action: runtime_artifacts.Action) -> dict[str, Any]:
    return {
        "op": action.op,
        "target": action.target,
        "summary": action.summary,
        "executed": action.executed,
    }


def _orphan_dict(orphan: runtime_artifacts.Orphan) -> dict[str, Any]:
    return {
        "kind": orphan.kind,
        "summary": orphan.summary,
        "port": orphan.port,
        "pid": orphan.pid,
        "path": str(orphan.path) if orphan.path is not None else None,
    }


# ---------------------------------------------------------------------------
# Human rendering
# ---------------------------------------------------------------------------


def _render_plan(result: dict[str, Any]) -> None:
    """Render the dry-run plan: would-do actions + advisory conflict notes."""
    print_rich("\n  [bold]Cleanup (dry run)[/bold]\n")

    plan: list[dict[str, Any]] = result.get("plan", [])
    conflicts: list[dict[str, Any]] = result.get("conflicts", [])

    if not plan and not conflicts:
        print_rich("  [green]No orphans found — nothing to clean up.[/green]\n")
        return

    for action in plan:
        print_rich(f"  would {action['op']}: {action['summary']}")
    for conflict in conflicts:
        print_rich(f"  [yellow]![/yellow] {conflict['summary']}")

    print_rich()


def _render_result(result: dict[str, Any]) -> None:
    """Render the result of an executed (or cancelled) reap."""
    print_rich("\n  [bold]Cleanup[/bold]\n")

    if result.get("cancelled"):
        print_rich("  [dim]cancelled[/dim]\n")
        return

    executed: list[dict[str, Any]] = result.get("executed", [])
    warnings: list[dict[str, Any]] = result.get("warnings", [])

    if not executed and not warnings:
        print_rich("  [green]No orphans found — nothing to clean up.[/green]\n")
        return

    for action in executed:
        print_rich(f"  [green]{action['op']}[/green]: {action['summary']}")
    for warning in warnings:
        print_rich(f"  [yellow]![/yellow] {warning['summary']}")

    print_rich()


# ---------------------------------------------------------------------------
# Confirmation prompt
# ---------------------------------------------------------------------------


def _confirm() -> bool:
    """Prompt ``Proceed? [y/N]``. EOF/Ctrl-C declines (safe default)."""
    try:
        raw = input("  Proceed? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)
        return False
    return raw in ("y", "yes")


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


def _run(args: argparse.Namespace) -> int:
    plan = [_action_dict(a) for a in runtime_artifacts.reap("all", dry_run=True)]
    conflicts = [
        _orphan_dict(o) for o in runtime_artifacts.detect_orphans() if o.kind == "conflict"
    ]

    if args.dry_run:
        result: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "dry_run": True,
            "plan": plan,
            "conflicts": conflicts,
        }
        write_result(result, args, human_fn=_render_plan)
        return 0

    # Not a dry run: show the plan, then confirm (unless --yes) before reaping.
    nothing_to_do = not plan and not conflicts
    if not args.yes:
        if not args.json:
            _render_plan({"schema_version": SCHEMA_VERSION, "plan": plan, "conflicts": conflicts})
        if nothing_to_do:
            result = {
                "schema_version": SCHEMA_VERSION,
                "dry_run": False,
                "cancelled": False,
                "executed": [],
                "warnings": [],
            }
            write_result(result, args, human_fn=_render_result)
            return 0
        if not _confirm():
            result = {
                "schema_version": SCHEMA_VERSION,
                "dry_run": False,
                "cancelled": True,
                "executed": [],
                "warnings": [],
            }
            write_result(result, args, human_fn=_render_result)
            return 0

    actions = [_action_dict(a) for a in runtime_artifacts.reap("all")]
    executed = [a for a in actions if a["op"] != "warn_foreign" and a["executed"]]
    warnings = [a for a in actions if a["op"] == "warn_foreign"]

    result = {
        "schema_version": SCHEMA_VERSION,
        "dry_run": False,
        "cancelled": False,
        "executed": executed,
        "warnings": warnings,
    }
    write_result(result, args, human_fn=_render_result)
    return 0
