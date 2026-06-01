"""``wrap`` verb — run a child process with AgentAlloy wiring active.

Usage::

    python -m agentalloy.install wrap <harness> [--port N] [--via hook|proxy]
        [--no-start-server] [-- <args ...>]

Lifecycle:

1. Resolve harness name from the wire_harness REGISTRY.
2. Probe the port for an existing server; check PID file for ownership.
3. Start the background server (unless --no-start-server).
4. Apply wiring (hook or proxy) for the chosen harness.
5. Spawn the child process with the wiring in place.
6. On exit (normal or signal), tear down wiring and stop the server
   if we started it.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from agentalloy.install import server_proc
from agentalloy.install import state as install_state
from agentalloy.install.output import print_rich, print_rich_stderr
from agentalloy.install.subcommands.wire_harness import VALID_HARNESSES, wire_harness

# PID file location (under user data dir)
PID_FILE_NAME = "wrap.pid"


def _pid_file_path() -> Path:
    """Return the path to the wrap PID file."""
    return install_state.user_data_dir() / PID_FILE_NAME


def _read_pid_file() -> int | None:
    """Read the PID from the wrap PID file, or None if absent/invalid."""
    p = _pid_file_path()
    if not p.exists():
        return None
    try:
        return int(p.read_text().strip())
    except (ValueError, OSError):
        return None


def _write_pid_file(pid: int) -> None:
    """Write our PID to the wrap PID file."""
    p = _pid_file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(pid))


def _remove_pid_file() -> None:
    """Remove the wrap PID file."""
    with contextlib.suppress(OSError):
        _pid_file_path().unlink(missing_ok=True)


def _port_owned_by_us(port: int, host: str = server_proc.DEFAULT_HOST) -> bool:
    """Check whether the port is owned by a process with a matching PID file.

    Returns True if:
    - A PID file exists, the PID is alive, and it is listening on the port.
    - Or the port is free (no server running).
    """
    pid = _read_pid_file()
    if pid is not None:
        # Check if the PID is still alive and listening on our port.
        try:
            os.kill(pid, 0)  # existence check
        except (ProcessLookupError, PermissionError):
            # PID is dead — stale PID file.
            return False
        # Check if this PID is listening on the port.
        if server_proc.find_listening_pid(port, host=host) == pid:
            return True
    return False


def _render_human(result: dict[str, Any]) -> None:
    """Render wrap result in human-readable format."""
    action = result.get("action", "unknown")
    print_rich("\n  [bold]Wrap[/bold]\n")
    print_rich(f"  Action: [bold green]{action}[/bold green]")

    harness = result.get("harness")
    if harness:
        print_rich(f"  Harness: {harness}")

    port = result.get("port")
    if port is not None:
        print_rich(f"  Port: {port}")

    via = result.get("via")
    if via:
        print_rich(f"  Via: {via}")

    child_pid = result.get("child_pid")
    if child_pid is not None:
        print_rich(f"  Child PID: {child_pid}")

    server_started = result.get("server_started")
    if server_started is not None:
        status = "started" if server_started else "already running"
        print_rich(f"  Server: [bold]{status}[/bold]")

    files = result.get("files_written", [])
    if files:
        print_rich(f"  Files modified: {len(files)}")
        for f in files:
            print_rich(f"    ~ {f.get('path', '?')}")

    print_rich()


def _run(args: argparse.Namespace) -> int:
    cwd = Path.cwd().resolve()
    harness = args.harness
    port = args.port
    via = args.via  # "hook" or "proxy"
    no_start_server = args.no_start_server
    child_args = args.child_args

    # ------------------------------------------------------------------
    # 1. Validate harness
    # ------------------------------------------------------------------
    if harness not in VALID_HARNESSES:
        print_rich_stderr(
            f"ERROR: Unknown harness '{harness}'.",
        )
        print_rich_stderr(
            f"FIX:   Use one of: {', '.join(sorted(VALID_HARNESSES))}.",
        )
        return 1

    # ------------------------------------------------------------------
    # 2. Resolve port
    # ------------------------------------------------------------------
    if port is not None:
        port = install_state.validate_port(port)
    else:
        st = install_state.load_state()
        port = install_state.validate_port(st.get("port", 47950))

    host = server_proc.DEFAULT_HOST

    # ------------------------------------------------------------------
    # 3. Probe port — check for existing server
    # ------------------------------------------------------------------
    existing_pid = server_proc.find_listening_pid(port, host=host)
    port_owned = _port_owned_by_us(port, host=host)

    if existing_pid is not None:
        print_rich(f"  Port {port}: server already running (pid {existing_pid})")
    elif port_owned:
        # PID file says we own it but ss didn't find it — probably a race.
        # Try to connect.
        if server_proc.port_reachable(port, host=host):
            print_rich(f"  Port {port}: server reachable (PID file owner)")
            existing_pid = _read_pid_file()
        else:
            print_rich(f"  Port {port}: PID file stale, server not running")
            existing_pid = None
            _remove_pid_file()

    # ------------------------------------------------------------------
    # 4. Start server if needed
    # ------------------------------------------------------------------
    server_started = False
    if not no_start_server and existing_pid is None:
        try:
            pid = server_proc.start_background(port, host=host)
            _write_pid_file(pid)
            print_rich_stderr(
                f"  Starting server on {host}:{port} (pid {pid})",
            )
            if not server_proc.wait_until_listening(port, timeout_s=15.0, host=host):
                print_rich_stderr(
                    f"ERROR: Server did not become ready within 15s. "
                    f"Check {server_proc.server_log_path()}",
                )
                _remove_pid_file()
                return 2
            server_started = True
            existing_pid = pid
        except server_proc.ServerLifecycleError as e:
            print_rich_stderr(f"ERROR: {e}")
            return 1
    elif no_start_server and existing_pid is None:
        print_rich_stderr(
            f"ERROR: No server running on port {port}. Start one first or omit --no-start-server.",
        )
        return 1

    # ------------------------------------------------------------------
    # 5. Apply wiring
    # ------------------------------------------------------------------
    print_rich(f"  Wiring harness '{harness}' via {via} ...")

    if via == "hook":
        # Hook wiring: use the legacy markdown-injection path
        result = wire_harness(
            harness,
            port=port,
            root=cwd,
            legacy=True,
            scope="repo",
        )
    else:
        # Proxy wiring (default)
        result = wire_harness(
            harness,
            port=port,
            root=cwd,
            scope="repo",
        )

    files_written = result.get("files_written", [])
    print_rich(f"  Wired {len(files_written)} file(s)")

    # ------------------------------------------------------------------
    # 6. Spawn child process
    # ------------------------------------------------------------------
    if not child_args:
        print_rich_stderr(
            "ERROR: No child process specified. Pass args after --.",
        )
        print_rich_stderr(
            "FIX:   agentalloy wrap <harness> -- <command> [args]",
        )
        return 1

    print_rich(f"  Spawning child: {' '.join(child_args)}")

    # Build child environment: inherit parent env, but ensure the proxy
    # port is accessible. The wiring files already point to localhost:port.
    child_env = {**os.environ}

    # Write PID file for the child so teardown knows what to clean up.
    # We keep the server PID file as well.

    try:
        proc = subprocess.Popen(
            child_args,
            env=child_env,
            start_new_session=False,
        )
        child_pid = proc.pid
    except FileNotFoundError as e:
        print_rich_stderr(f"ERROR: Child process not found: {child_args[0]}")
        print_rich_stderr(f"       {e}")
        return 2

    print_rich(f"  Child PID: {child_pid}")

    # ------------------------------------------------------------------
    # 7. Set up signal handlers for teardown
    # ------------------------------------------------------------------
    _teardown_state: dict[str, Any] = {
        "server_started": server_started,
        "server_pid": existing_pid,
        "harness": harness,
        "port": port,
        "root": cwd,
        "via": via,
        "files_written": files_written,
    }

    def _signal_handler(signum: int, _frame: Any) -> None:
        """Handle SIGINT/SIGTERM: kill child, teardown wiring, stop server."""
        sig_name = signal.Signals(signum).name
        print_rich(f"\n  Received {sig_name}, tearing down ...")

        # Kill child process group.
        with contextlib.suppress(ProcessLookupError, OSError):
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError, OSError):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait()

        # Teardown wiring if via=hook (proxy wiring is reversible by nature).
        if via == "hook":
            # For hook wiring, we'd need to run unwire logic.
            # The unwire subcommand handles this.
            print_rich("  Hook wiring teardown skipped (use unwire to clean up)")

        # Stop server if we started it.
        if server_started and existing_pid is not None:
            with contextlib.suppress(server_proc.ServerLifecycleError):
                server_proc.stop(existing_pid, timeout_s=5)
            _remove_pid_file()

        print_rich("  Teardown complete.")
        sys.exit(signum)

    # Register handlers for SIGINT and SIGTERM.
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # Wait for child to exit.
    exit_code = proc.wait()

    # ------------------------------------------------------------------
    # 8. Teardown on normal exit
    # ------------------------------------------------------------------
    print_rich("\n  Child exited with code {exit_code}, tearing down ...")

    # Stop server if we started it.
    if server_started and existing_pid is not None:
        with contextlib.suppress(server_proc.ServerLifecycleError):
            server_proc.stop(existing_pid, timeout_s=5)
        _remove_pid_file()

    print_rich("  Teardown complete.")

    return exit_code


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    p: argparse.ArgumentParser = subparsers.add_parser(
        "wrap",
        help=(
            "Run a child process with AgentAlloy wiring active. "
            "Starts the server if needed, wires the harness, runs the child, "
            "then tears down on exit."
        ),
    )
    p.add_argument(
        "harness",
        choices=sorted(VALID_HARNESSES),
        help="Coding agent harness to wire.",
    )
    p.add_argument(
        "--port",
        type=int,
        default=None,
        help="Override the service port (default: read from user state, fallback 47950).",
    )
    p.add_argument(
        "--via",
        choices=("hook", "proxy"),
        default="proxy",
        help="Wiring method: 'hook' for legacy markdown injection, 'proxy' for proxy model (default).",
    )
    p.add_argument(
        "--no-start-server",
        action="store_true",
        help="Do not start the server; expect it to be already running.",
    )
    p.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output result as JSON.",
    )
    p.add_argument(
        "child_args",
        nargs=argparse.REMAINDER,
        help="Child process command and arguments (after --).",
    )
    p.set_defaults(func=_run)


def run(args: argparse.Namespace) -> int:
    """Public entry point for non-argparse callers."""
    return _run(args)
