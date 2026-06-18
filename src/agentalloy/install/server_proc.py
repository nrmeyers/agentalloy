"""Server-process helpers — detect, start, stop the agentalloy uvicorn.

The ``serve`` subcommand runs uvicorn in the foreground. The ``server-*``
subcommands manage a background instance using these helpers.

Detection is port-based (parses ``ss -tlnpH``) rather than PID-file based
so unclean exits don't leave us pointing at a stale PID, and so a
manually-launched uvicorn on the configured port is still discoverable.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from agentalloy.install import state as install_state

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT_FALLBACK = 47950
STOP_POLL_INTERVAL_S = 0.2
START_POLL_INTERVAL_S = 0.2


@dataclass(frozen=True)
class ServerInfo:
    port: int
    pid: int | None
    reachable: bool


class ServerLifecycleError(RuntimeError):
    """User-correctable lifecycle failures (port in use, no such process, etc.)."""


def configured_port() -> int:
    """Read the configured server port from install state; fall back to 47950."""
    st = install_state.load_state()
    return install_state.validate_port(st.get("port", DEFAULT_PORT_FALLBACK))


def find_listening_pid(port: int, host: str = DEFAULT_HOST) -> int | None:
    """Return the PID of a process LISTENing on ``host:port``, or None.

    Uses ``ss -tlnpH sport = :<port>`` and parses the first ``pid=<n>`` it
    finds. ``ss`` is part of iproute2 and is present on every modern Linux
    distribution; no Python dependency is added.
    """
    try:
        result = subprocess.run(
            ["ss", "-tlnpH", "sport", "=", f":{port}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, UnicodeDecodeError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        # Filter to lines actually bound to the target host:port; ss can
        # surface IPv6 wildcards (`*:47950`) or other hosts when the sport
        # filter matches a range.
        if f":{port}" not in line:
            continue
        if host not in line and "*:" not in line and "0.0.0.0:" not in line:
            continue
        m = re.search(r"pid=(\d+)", line)
        if m:
            return int(m.group(1))
    return None


def port_reachable(port: int, host: str = DEFAULT_HOST, timeout_s: float = 1.0) -> bool:
    """TCP-connect probe. True if the port accepts connections."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout_s)
            return s.connect_ex((host, port)) == 0
    except OSError:
        return False


def _read_cmdline(pid: int) -> str:
    """Return ``/proc/<pid>/cmdline`` as a space-joined string ('' if unreadable).

    The file is NUL-separated; we join args with spaces for substring matching.
    """
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()


def reclaim_stale_port(
    port: int, match_substrings: list[str], host: str = DEFAULT_HOST
) -> int | None:
    """Kill a STALE AgentAlloy/llama-server process squatting on ``host:port``.

    Only kills the listener when its ``/proc`` cmdline contains *all* of
    ``match_substrings`` — i.e. it is unambiguously one of our own processes
    (e.g. ``llama-server`` running our embed model, or ``uvicorn agentalloy.app``).
    A foreign process holding the port is left untouched. This is what lets
    ``enable-service``/restart self-heal after ``uv tool install --force`` leaves
    an old service or llama-server squatting a port — without ever killing an
    unrelated process bound to it.

    Returns the reclaimed PID, or None if the port is free or held by something
    that does not match. Best-effort: a failed ``stop()`` returns None.
    """
    if not match_substrings:  # never kill an arbitrary holder
        return None
    pid = find_listening_pid(port, host=host)
    if pid is None:
        return None
    cmdline = _read_cmdline(pid)
    if not cmdline or not all(s in cmdline for s in match_substrings):
        return None  # free, or a foreign holder — leave it alone
    try:
        stop(pid)
    except ServerLifecycleError:
        return None
    logger.info("reclaimed stale port %d from pid %d (matched %s)", port, pid, match_substrings)
    return pid


def server_info(port: int | None = None, host: str = DEFAULT_HOST) -> ServerInfo:
    """Snapshot of the configured server's state."""
    p = port if port is not None else configured_port()
    return ServerInfo(
        port=p,
        pid=find_listening_pid(p, host=host),
        reachable=port_reachable(p, host=host),
    )


def server_log_path() -> Path:
    """Where background-mode uvicorn writes stdout/stderr."""
    return install_state.user_data_dir() / "server.log"


def start_background(
    port: int,
    host: str = DEFAULT_HOST,
    *,
    env: dict[str, str] | None = None,
) -> int:
    """Spawn uvicorn detached. Returns the child PID.

    Refuses to start if the port is already bound. Caller is responsible
    for verifying readiness with ``wait_until_listening``.

    Loads the user-scope ``.env`` into the child's environment using the
    same logic as the ``serve`` foreground path so the two produce
    identical runtime configurations.
    """
    existing = find_listening_pid(port, host=host)
    if existing is not None:
        raise ServerLifecycleError(
            f"port {host}:{port} is already bound by pid {existing}; "
            "stop it first or pick another port"
        )

    log_path = server_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Append, not overwrite — preserves prior session output for triage.
    log = open(log_path, "ab", buffering=0)  # noqa: SIM115 — handed to subprocess

    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "agentalloy.app:app",
        "--host",
        host,
        "--port",
        str(port),
    ]
    # Build the child env: process env, then the user .env (without
    # overriding anything already set in the parent shell — matches
    # pydantic-settings priority), then any caller overrides.
    child_env = {**os.environ}
    for key, val in install_state.parse_env_file().items():
        child_env.setdefault(key, val)
    if env:
        child_env.update(env)

    proc = subprocess.Popen(
        cmd,
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        env=child_env,
    )
    log.close()
    return proc.pid


def wait_until_listening(port: int, timeout_s: float, host: str = DEFAULT_HOST) -> bool:
    """Poll the port; return True if it accepts connections within ``timeout_s``."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if port_reachable(port, host=host):
            return True
        time.sleep(START_POLL_INTERVAL_S)
    return False


def stop(pid: int, timeout_s: float = 10.0) -> str:
    """SIGTERM the pid; escalate to SIGKILL after ``timeout_s``.

    Returns ``"term"`` if the process exited from SIGTERM, ``"kill"`` if
    it required SIGKILL. Raises ``ServerLifecycleError`` if the pid does
    not exist.
    """
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError as e:
        raise ServerLifecycleError(f"no process with pid {pid}") from e
    except PermissionError as e:
        raise ServerLifecycleError(f"permission denied sending SIGTERM to pid {pid}") from e

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return "term"
        time.sleep(STOP_POLL_INTERVAL_S)

    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        # Raced with natural exit.
        return "term"
    # Brief follow-up wait for the kernel to reap.
    time.sleep(STOP_POLL_INTERVAL_S * 2)
    return "kill"


def _pid_alive(pid: int) -> bool:
    """True iff ``pid`` exists and is not a zombie.

    ``os.kill(pid, 0)`` returns success for zombies (terminated but unreaped
    children of the caller), which would make ``stop()`` incorrectly escalate
    to SIGKILL. We read ``/proc/<pid>/status`` and treat state ``Z`` as dead.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # Process table entry exists; check it's not a zombie.
    try:
        status = Path(f"/proc/{pid}/status").read_text()
    except (FileNotFoundError, PermissionError):
        return True
    for line in status.splitlines():
        if line.startswith("State:"):
            return "Z" not in line
    return True
