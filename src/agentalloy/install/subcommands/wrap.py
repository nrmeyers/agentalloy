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
import json
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
from agentalloy.providers import REGISTRY

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
    from agentalloy.install.subcommands.wire import apply_hook_wiring, resolve_via

    cwd = Path.cwd().resolve()
    harness = args.harness
    port = args.port
    via = resolve_via(harness, getattr(args, "via", None))  # "hook" or "proxy"
    no_start_server = args.no_start_server
    child_args = args.child_args
    json_output = getattr(args, "json", False)

    # Helper: route human-readable output to stderr in --json mode so stdout
    # contains only JSON (machine-parseable).
    def _out(msg: str) -> None:
        if json_output:
            print_rich_stderr(msg)
        else:
            print_rich(msg)

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
        _out(f"  Port {port}: server already running (pid {existing_pid})")
    elif port_owned:
        # PID file says we own it but ss didn't find it — probably a race.
        # Try to connect.
        if server_proc.port_reachable(port, host=host):
            _out(f"  Port {port}: server reachable (PID file owner)")
            existing_pid = _read_pid_file()
        else:
            _out(f"  Port {port}: PID file stale, server not running")
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
    _out(f"  Wiring harness '{harness}' via {via} ...")

    if via == "hook":
        # Hook wiring: install the hook script + merge settings.json via the
        # provider hook_writer. Graceful-degradation default for claude-code.
        result = apply_hook_wiring(harness, port=port, root=cwd)
    else:
        # Proxy wiring (opt-in for claude-code; default elsewhere).
        result = wire_harness(
            harness,
            port=port,
            root=cwd,
            scope="repo",
        )

    files_written = result.get("files_written", [])
    _out(f"  Wired {len(files_written)} file(s)")

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

    _out(f"  Spawning child: {' '.join(child_args)}")

    # Build child environment: inherit parent env, then apply the provider's
    # env_builder (ANTHROPIC_BASE_URL / OPENAI_BASE_URL / ... → the proxy).
    # Without this, env-based harnesses (claude-code, codex, openclaw,
    # opencode) launch pointing at their real upstreams and the proxy never
    # sees a request — the wiring env files on disk are not sourced by
    # anything in the spawn path. Explicit user overrides win: a var already
    # present in the parent env is left untouched.
    child_env = {**os.environ}
    spec = REGISTRY.get(harness)
    # Only inject proxy env vars (ANTHROPIC_BASE_URL / OPENAI_BASE_URL / ...)
    # for proxy wiring. Hook wiring must NOT redirect the harness's base URL —
    # the whole point is that the harness talks to its real upstream and the
    # hook script carries the signal layer out-of-band.
    if via == "proxy" and spec is not None:
        for key, value in spec.env_builder(port).items():
            if key not in os.environ:
                child_env[key] = value
            else:
                _out(f"  [dim]env: keeping caller's {key} (overrides wiring)[/dim]")

    # Write PID file for the child so teardown knows what to clean up.
    # We keep the server PID file as well.

    try:
        # start_new_session=True puts the child in its own process group so the
        # SIGINT/SIGTERM handler below can call os.killpg on the child's group
        # without also terminating this wrapper or the invoking shell.
        proc = subprocess.Popen(
            child_args,
            env=child_env,
            start_new_session=True,
        )
        child_pid = proc.pid
    except FileNotFoundError as e:
        print_rich_stderr(f"ERROR: Child process not found: {child_args[0]}")
        print_rich_stderr(f"       {e}")
        return 2

    _out(f"  Child PID: {child_pid}")

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
        _out(f"\n  Received {sig_name}, tearing down ...")

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
            _out("  Hook wiring teardown skipped (use unwire to clean up)")

        # Stop server if we started it.
        if server_started and existing_pid is not None:
            with contextlib.suppress(server_proc.ServerLifecycleError):
                server_proc.stop(existing_pid, timeout_s=5)
            _remove_pid_file()

        _out("  Teardown complete.")
        sys.exit(signum)

    # Register handlers for SIGINT and SIGTERM.
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # Wait for child to exit.
    exit_code = proc.wait()

    # ------------------------------------------------------------------
    # 8. Teardown on normal exit
    # ------------------------------------------------------------------
    _out(f"\n  Child exited with code {exit_code}, tearing down ...")

    # Stop server if we started it.
    if server_started and existing_pid is not None:
        with contextlib.suppress(server_proc.ServerLifecycleError):
            server_proc.stop(existing_pid, timeout_s=5)
        _remove_pid_file()

    _out("  Teardown complete.")

    # ------------------------------------------------------------------
    # 9. JSON output (if --json requested)
    # ------------------------------------------------------------------
    if json_output:
        result = {
            "action": "wrap_complete",
            "harness": harness,
            "port": port,
            "via": via,
            "child_pid": child_pid,
            "server_started": server_started,
            "child_exit_code": exit_code,
            "files_written": files_written,
        }
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")

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
    # Optional flags must come before positional args when child_args uses REMAINDER
    p.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output raw JSON instead of human-readable text.",
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
        default=None,
        help=(
            "Wiring method. Default resolves per harness: 'hook' for claude-code "
            "(degrades gracefully), 'proxy' for everything else. 'hook' installs the "
            "hook script + merges settings.json (no base-URL redirection); 'proxy' "
            "rewrites the harness base URL to the local proxy."
        ),
    )
    p.add_argument(
        "--no-start-server",
        action="store_true",
        help="Do not start the server; expect it to be already running.",
    )
    p.add_argument(
        "harness",
        choices=sorted(VALID_HARNESSES),
        help="Coding agent harness to wire.",
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
