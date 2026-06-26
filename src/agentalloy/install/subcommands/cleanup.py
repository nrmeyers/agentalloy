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

``--deep`` (alias ``--all``) escalates to a full, *state-independent* host
sanitize via :mod:`agentalloy.install.host_sanitize`: it removes every AgentAlloy
runtime, data/config directory, container artifact, and per-repo proxy carrier it
can find by known location — for testers who need a true blank slate. Pre-existing
llama-servers are never touched. The bare ``cleanup`` behaviour is unchanged.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from agentalloy.install import host_sanitize, runtime_artifacts
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
    p.add_argument(
        "--deep",
        "--all",
        dest="deep",
        action="store_true",
        default=False,
        help=(
            "Full host sanitize: state-independently remove ALL agentalloy runtimes, "
            "data, config, containers, and per-repo proxy wiring. Pre-existing "
            "llama-servers are never touched."
        ),
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
    if args.deep:
        return _run_deep(args)

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


# ---------------------------------------------------------------------------
# Deep host sanitize (``--deep`` / ``--all``)
# ---------------------------------------------------------------------------


def _render_cli_hint(result: dict[str, Any]) -> None:
    hint = result.get("cli_hint")
    if hint:
        print_rich(
            f"\n  [dim]The agentalloy CLI itself was left installed. To remove it: {hint}[/dim]"
        )


def _render_deep_plan(result: dict[str, Any]) -> None:
    """Render the deep dry-run plan: every state-independent removal we would do."""
    print_rich("\n  [bold]Deep cleanup — host sanitize (dry run)[/bold]\n")

    plan: list[dict[str, Any]] = result.get("plan", [])
    warnings: list[str] = result.get("warnings", [])

    if not plan and not warnings:
        print_rich("  [green]Nothing found — host is already clean.[/green]\n")
        _render_cli_hint(result)
        return

    for action in plan:
        print_rich(f"  would {action['op']}: {action['summary']}")
    for warning in warnings:
        print_rich(f"  [yellow]![/yellow] {warning}")

    _render_cli_hint(result)
    print_rich()


def _render_deep_result(result: dict[str, Any]) -> None:
    """Render the result of an executed (or cancelled) deep sanitize."""
    print_rich("\n  [bold]Deep cleanup — host sanitize[/bold]\n")

    if result.get("cancelled"):
        print_rich("  [dim]cancelled[/dim]\n")
        return

    executed: list[dict[str, Any]] = result.get("executed", [])
    warnings: list[str] = result.get("warnings", [])

    if not executed and not warnings:
        print_rich("  [green]Nothing to remove — host is already clean.[/green]\n")
        _render_cli_hint(result)
        return

    for action in executed:
        print_rich(f"  [green]{action['op']}[/green]: {action['summary']}")
    for warning in warnings:
        print_rich(f"  [yellow]![/yellow] {warning}")

    _render_cli_hint(result)
    print_rich()


def _run_deep(args: argparse.Namespace) -> int:
    """Full, state-independent host sanitize. Destructive — confirm unless ``--yes``."""
    plan_report = host_sanitize.sanitize(dry_run=True, scan_home=True)
    plan = [_action_dict(a) for a in plan_report.actions]

    if args.dry_run:
        result: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "deep": True,
            "dry_run": True,
            "plan": plan,
            "warnings": plan_report.warnings,
            "cli_hint": plan_report.cli_hint,
        }
        write_result(result, args, human_fn=_render_deep_plan)
        return 0

    nothing_to_do = not plan and not plan_report.warnings
    if not args.yes:
        if not args.json:
            _render_deep_plan(
                {
                    "schema_version": SCHEMA_VERSION,
                    "plan": plan,
                    "warnings": plan_report.warnings,
                    "cli_hint": plan_report.cli_hint,
                }
            )
        if nothing_to_do:
            result = {
                "schema_version": SCHEMA_VERSION,
                "deep": True,
                "dry_run": False,
                "cancelled": False,
                "executed": [],
                "warnings": [],
                "cli_hint": plan_report.cli_hint,
            }
            write_result(result, args, human_fn=_render_deep_result)
            return 0
        if not _confirm():
            result = {
                "schema_version": SCHEMA_VERSION,
                "deep": True,
                "dry_run": False,
                "cancelled": True,
                "executed": [],
                "warnings": [],
                "cli_hint": plan_report.cli_hint,
            }
            write_result(result, args, human_fn=_render_deep_result)
            return 0

    live = host_sanitize.sanitize(dry_run=False, scan_home=True)
    executed = [_action_dict(a) for a in live.actions if a.executed]
    result = {
        "schema_version": SCHEMA_VERSION,
        "deep": True,
        "dry_run": False,
        "cancelled": False,
        "executed": executed,
        "warnings": live.warnings,
        "cli_hint": live.cli_hint,
    }
    write_result(result, args, human_fn=_render_deep_result)
    return 0
