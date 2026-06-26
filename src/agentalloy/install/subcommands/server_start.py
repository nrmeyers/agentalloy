"""``server-start`` verb — launch uvicorn in the background.

Foreground use is still ``serve``. ``server-start`` exists so users can
keep working in the same shell while the server runs, and so other
subcommands (and tests) have a programmatic way to bring the service up.
"""

from __future__ import annotations

import argparse
import sys

from agentalloy.install import server_proc
from agentalloy.install.subcommands import server_container

EXIT_OK = 0
EXIT_USER = 1
EXIT_SYSTEM = 2


def _print_release_notice() -> None:
    """Surface a cached new-release nudge (+ a one-time opt-out hint) on stderr
    after a successful start. Cache-only, best-effort, never raises.
    """
    try:
        from agentalloy.install import release_check  # noqa: PLC0415 — keep off the cold path

        if release_check.take_optout_notice():
            print(
                "server-start: agentalloy checks for new releases (~daily); "
                "disable with AGENTALLOY_RELEASE_CHECK=0",
                file=sys.stderr,
            )
        info = release_check.notice()
        if info:
            print(
                f"server-start: agentalloy {info['latest'].lstrip('v')} available "
                f"({info['bump_type']}) — run 'agentalloy upgrade'",
                file=sys.stderr,
            )
    except Exception:
        pass


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    p: argparse.ArgumentParser = subparsers.add_parser(
        "server-start",
        help="Launch the agentalloy service in the background.",
    )
    p.add_argument("--port", type=int, default=None, help="Override configured port.")
    p.add_argument(
        "--host",
        default=server_proc.DEFAULT_HOST,
        help="Bind address (default: 127.0.0.1).",
    )
    p.add_argument(
        "--wait",
        type=float,
        default=15.0,
        help="Seconds to wait for the port to start accepting connections.",
    )
    p.set_defaults(func=_run)


def _run(args: argparse.Namespace) -> int:
    target = server_proc.resolve_deployment(args.port)
    if target.deployment == "container":
        return _run_container(args, target)

    port = target.port

    try:
        pid = server_proc.start_background(port, host=args.host)
    except server_proc.ServerLifecycleError as e:
        print(f"server-start: {e}", file=sys.stderr)
        return EXIT_USER

    print(
        f"server-start: launched pid {pid} on {args.host}:{port}; "
        f"waiting up to {args.wait:.1f}s for readiness",
        file=sys.stderr,
    )

    if not server_proc.wait_until_listening(port, args.wait, host=args.host):
        print(
            f"server-start: pid {pid} did not start listening within "
            f"{args.wait:.1f}s; check {server_proc.server_log_path()}",
            file=sys.stderr,
        )
        return EXIT_SYSTEM

    print(
        f"server-start: ready on {args.host}:{port} (pid {pid}, log "
        f"{server_proc.server_log_path()})",
        file=sys.stderr,
    )
    _print_release_notice()
    return EXIT_OK


def _run_container(args: argparse.Namespace, target: server_proc.DeploymentTarget) -> int:
    """Container leg: start the (baked-entrypoint) container and await /health."""
    code, payload = server_container.run_start(target, wait=args.wait)
    action = payload.get("action")
    if action == "already_running":
        print(
            f"server-start: container '{target.container_name}' already running on :{target.port}",
            file=sys.stderr,
        )
    elif action == "started":
        print(
            f"server-start: started container '{target.container_name}'; ready on :{target.port}",
            file=sys.stderr,
        )
    if payload.get("error"):
        print(f"server-start: {payload['error']}", file=sys.stderr)
    elif code == EXIT_OK:
        _print_release_notice()
    return code
