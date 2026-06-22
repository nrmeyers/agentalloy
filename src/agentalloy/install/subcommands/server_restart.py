"""``server-restart`` verb — stop (if running) and start in the background."""

from __future__ import annotations

import argparse
import sys

from agentalloy.install import server_proc

EXIT_OK = 0
EXIT_USER = 1
EXIT_SYSTEM = 2


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    p: argparse.ArgumentParser = subparsers.add_parser(
        "server-restart",
        help="Restart the background agentalloy service.",
        description=(
            "Restart the agentalloy service. In container mode this restarts the "
            "uvicorn process *inside* the existing container — it does NOT change "
            "container-level spec (mounts, env, published ports). To pick up a new "
            "projects-root mount or image, recreate the container with "
            "`agentalloy upgrade` instead."
        ),
    )
    p.add_argument("--port", type=int, default=None, help="Override configured port.")
    p.add_argument(
        "--host",
        default=server_proc.DEFAULT_HOST,
        help="Bind address (default: 127.0.0.1).",
    )
    p.add_argument(
        "--stop-timeout",
        type=float,
        default=10.0,
        help="Seconds to wait after SIGTERM before SIGKILL.",
    )
    p.add_argument(
        "--wait",
        type=float,
        default=15.0,
        help="Seconds to wait for readiness after start.",
    )
    p.set_defaults(func=_run)


def _run(args: argparse.Namespace) -> int:
    port = args.port if args.port is not None else server_proc.configured_port()

    pid = server_proc.find_listening_pid(port, host=args.host)
    if pid is not None:
        try:
            outcome = server_proc.stop(pid, timeout_s=args.stop_timeout)
        except server_proc.ServerLifecycleError as e:
            print(f"server-restart: stop failed: {e}", file=sys.stderr)
            return EXIT_USER
        print(
            f"server-restart: stopped pid {pid} via SIG{outcome.upper()}",
            file=sys.stderr,
        )
    else:
        print(f"server-restart: nothing was listening on :{port}", file=sys.stderr)

    try:
        new_pid = server_proc.start_background(port, host=args.host)
    except server_proc.ServerLifecycleError as e:
        print(f"server-restart: start failed: {e}", file=sys.stderr)
        return EXIT_USER

    if not server_proc.wait_until_listening(port, args.wait, host=args.host):
        print(
            f"server-restart: pid {new_pid} did not start listening within "
            f"{args.wait:.1f}s; check {server_proc.server_log_path()}",
            file=sys.stderr,
        )
        return EXIT_SYSTEM

    print(
        f"server-restart: ready on {args.host}:{port} (pid {new_pid})",
        file=sys.stderr,
    )
    return EXIT_OK
