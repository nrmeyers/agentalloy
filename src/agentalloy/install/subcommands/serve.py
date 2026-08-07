# pyright: reportPrivateUsage=false
"""``serve`` verb — run the AgentAlloy service in the foreground.

Sources the user-scope ``.env`` into the process environment, then
execs uvicorn against ``agentalloy.app:app``. Foreground / blocking, in
the local-LLM-tooling idiom (``ollama serve``, ``lm studio``). Daemon
supervision is intentionally out of scope — users start it in a terminal
and leave it running, or wrap it in their own systemd / launchd unit.
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys

from agentalloy.install import state as install_state


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    p: argparse.ArgumentParser = subparsers.add_parser(
        "serve",
        help="Run the AgentAlloy service (foreground uvicorn).",
    )
    p.add_argument(
        "--port",
        type=int,
        default=None,
        help="Override the service port (default: read from user state, fallback 47950).",
    )
    p.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address (default: 127.0.0.1 — loopback only).",
    )
    p.add_argument(
        "--reload",
        action="store_true",
        help="Enable uvicorn auto-reload (development only).",
    )
    p.set_defaults(func=_run)


def _run(args: argparse.Namespace) -> int:
    # 1. Load the user-scope .env into os.environ via the shared helper
    # in install.state — same code path as ``server-start``.
    loaded_keys = install_state.load_env_into_environ()
    if loaded_keys:
        print(
            f"agentalloy serve: loaded {len(loaded_keys)} keys from {install_state.env_path()}",
            file=sys.stderr,
        )
    else:
        print(
            f"agentalloy serve: no .env found at {install_state.env_path()} — "
            "running with process environment only. Run "
            "`python -m agentalloy.install setup` if you haven't yet.",
            file=sys.stderr,
        )

    # 2. Resolve port: CLI flag > state > default 47950.
    if args.port is not None:
        port = install_state.validate_port(args.port)
    else:
        st = install_state.load_state()
        port = install_state.validate_port(st.get("port", 47950))

    # 3. Exec uvicorn so signals (SIGINT) propagate cleanly.
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "agentalloy.app:app",
        "--host",
        args.host,
        "--port",
        str(port),
    ]
    if args.reload:
        cmd.append("--reload")

    print(
        f"agentalloy serve: exec {shlex.join(cmd)}",
        file=sys.stderr,
        flush=True,
    )
    # os.execvp replaces this process with uvicorn — no return on success.
    try:
        os.execvp(cmd[0], cmd)
    except FileNotFoundError:
        print(
            f"ERROR: {cmd[0]} not found. Is the agentalloy venv active?",
            file=sys.stderr,
        )
        return 2
    # Unreachable after execvp; kept for type-checker.
    return 0
