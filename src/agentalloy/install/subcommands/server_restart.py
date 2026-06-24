"""``server-restart`` verb.

Native: stop (if running) and relaunch uvicorn in the background.
Container: ``{runtime} restart <container>`` then wait for /health on the
mapped port (the container runs the image's baked entrypoint, so an in-place
restart survives — no recreate needed)."""

from __future__ import annotations

import argparse
import sys

from agentalloy.install import server_proc
from agentalloy.install.subcommands import server_container

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
            "Restart the agentalloy service. On a native install this stops the "
            "background uvicorn (if running) and relaunches it. On a container "
            "deployment it runs `{runtime} restart <container>` — restarting the "
            "existing container in place — then waits for /health on the mapped "
            "port. It does NOT change container-level spec (mounts, env, "
            "published ports); to pick up a new projects-root mount or image, "
            "recreate the container with `agentalloy upgrade` instead."
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
    target = server_proc.resolve_deployment(args.port)
    if target.deployment == "container":
        return _run_container(args, target)

    port = target.port

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


def _run_container(args: argparse.Namespace, target: server_proc.DeploymentTarget) -> int:
    """Container leg: ``{runtime} restart`` (or start if stopped) + /health wait."""
    code, payload = server_container.run_restart(
        target, stop_timeout=args.stop_timeout, wait=args.wait
    )
    action = payload.get("action")
    if action in ("restarted", "started"):
        print(
            f"server-restart: {action} container '{target.container_name}'; "
            f"ready on :{target.port}",
            file=sys.stderr,
        )
    if payload.get("error"):
        print(f"server-restart: {payload['error']}", file=sys.stderr)
    return code
