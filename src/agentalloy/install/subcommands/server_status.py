"""``server-status`` verb — report background server lifecycle state.

Lifecycle-focused: port, pid (if listening), reachability. The broader
``status`` verb covers install state and wired repos.
"""

from __future__ import annotations

import argparse
from typing import Any

from agentalloy.install import server_proc
from agentalloy.install.output import add_json_flag, write_result
from agentalloy.install.subcommands import server_container


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    p: argparse.ArgumentParser = subparsers.add_parser(
        "server-status",
        help="Report agentalloy server lifecycle state (port, pid, reachable).",
    )
    p.add_argument("--port", type=int, default=None, help="Override configured port.")
    add_json_flag(p)
    p.set_defaults(func=_run)


def _render_human(payload: dict[str, Any]) -> None:
    """Render server status in human-readable format (native + container)."""
    from agentalloy.install.output import print_rich

    print_rich("\n  [bold]Server Status[/bold]\n")

    port = payload.get("port", "N/A")
    reachable = payload.get("reachable", False)

    print_rich(f"  Port: {port}")

    if payload.get("deployment") == "container":
        # Container leg: report runtime + container state instead of host PID.
        print_rich(f"  Mode: container ({payload.get('runtime', 'unknown')})")
        print_rich(f"  Container: {payload.get('container', 'agentalloy')}")
        print_rich(f"  State: {payload.get('state', 'unknown')}")
    else:
        pid = payload.get("pid")
        if pid is not None:
            print_rich(f"  PID:  {pid}")
        else:
            print_rich("  PID:  [dim]no process[/dim]")

    reach_status = "[green]reachable[/green]" if reachable else "[red]not reachable[/red]"
    print_rich(f"  Status: {reach_status}")

    if payload.get("error"):
        print_rich(f"  [red]error[/red] {payload['error']}")

    if payload.get("deployment") != "container":
        log_path = payload.get("log_path", "N/A")
        print_rich(f"  Log:  {log_path}")

    print_rich()


def _run(args: argparse.Namespace) -> int:
    target = server_proc.resolve_deployment(args.port)
    if target.deployment == "container":
        payload = server_container.status(target)
        write_result(payload, args, human_fn=_render_human)
        return 0

    info = server_proc.server_info(port=args.port)
    payload = {
        "port": info.port,
        "pid": info.pid,
        "reachable": info.reachable,
        "log_path": str(server_proc.server_log_path()),
    }
    write_result(payload, args, human_fn=_render_human)
    return 0
