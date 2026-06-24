"""``server-stop`` verb — terminate the agentalloy server holding the corpus lock.

Detection is port-based; a manually-launched uvicorn on the configured
port is still discoverable. SIGTERM first; SIGKILL after ``--timeout``.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from agentalloy.install import server_proc
from agentalloy.install.output import add_json_flag, write_result
from agentalloy.install.subcommands import server_container

EXIT_OK = 0
EXIT_USER = 1


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    p: argparse.ArgumentParser = subparsers.add_parser(
        "server-stop",
        help=(
            "Stop whatever process is listening on the configured port "
            "(releases the corpus lock). Does not verify the process is "
            "agentalloy — on a shared port that's the operator's concern."
        ),
    )
    p.add_argument("--port", type=int, default=None, help="Override configured port.")
    p.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="Seconds to wait after SIGTERM before escalating to SIGKILL.",
    )
    add_json_flag(p)
    p.set_defaults(func=_run)


def _render_human(payload: dict[str, Any]) -> None:
    """Render server-stop result in human-readable format."""
    from agentalloy.install.output import render_action_result

    render_action_result(payload, title="Server Stop")


def _run(args: argparse.Namespace) -> int:
    target = server_proc.resolve_deployment(args.port)
    if target.deployment == "container":
        code, payload = server_container.run_stop(target, timeout=args.timeout)
        if code == EXIT_USER and payload.get("error"):
            print(f"server-stop: {payload['error']}", file=sys.stderr)
            return code
        write_result(payload, args, human_fn=_render_human)
        return code

    port = target.port
    pid = server_proc.find_listening_pid(port)
    if pid is None:
        # Nothing listening is the desired post-condition of stop —
        # report success so scripts and the setup composer don't treat
        # idempotent re-runs as failure. The "already_stopped" action
        # lets callers distinguish if they care.
        payload = {"action": "already_stopped", "port": port}
        write_result(payload, args, human_fn=_render_human)
        return EXIT_OK

    try:
        outcome = server_proc.stop(pid, timeout_s=args.timeout)
    except server_proc.ServerLifecycleError as e:
        print(f"server-stop: {e}", file=sys.stderr)
        return EXIT_USER

    payload = {"action": "stopped", "port": port, "pid": pid, "signal": outcome.upper()}
    write_result(payload, args, human_fn=_render_human)
    return EXIT_OK
