"""Container-aware database lock resolution helpers.

Provides functions for detecting container environments, stopping/starting
the uvicorn service, and testing corpus DB write-lock release — all needed
for the container-aware DuckDB lock resolution mechanism (TASK-1).
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from pathlib import Path

from agentalloy.install import server_proc
from agentalloy.install import state as install_state

_DEFAULT_UVICORN_CMD = "uv run uvicorn agentalloy.app:app --host 0.0.0.0 --port 47950"


def is_in_container() -> bool:
    """Return True if the process is running inside a container.

    Detects containers by checking for the Docker sentinel file ``/.dockerenv``
    or the presence of the ``/app`` directory, consistent with
    ``agentalloy.app:app`` (line 62).
    """
    try:
        if Path("/.dockerenv").exists():
            return True
    except OSError:
        pass
    try:
        if Path("/app").is_dir():
            return True
    except OSError:
        pass
    return False


def _find_uvicorn_pid() -> int | None:
    """Scan /proc for the AgentAlloy uvicorn process.

    Collects ALL matching PIDs and returns the minimum (parent) to avoid
    signaling a worker when the parent is still alive. ``iterdir()`` order
    is not guaranteed; first-match would be non-deterministic under reload
    or multi-worker deployments.

    Returns None if no matching process is found.
    """
    proc_dir = Path("/proc")
    if not proc_dir.is_dir():
        return None

    # P10-R2: bounded by OS /proc entries; ≤5 agentalloy uvicorn procs in practice
    pids: list[int] = []
    for pid_str in proc_dir.iterdir():  # P10-R2: single pass — OS-bounded
        if not pid_str.is_dir():
            continue
        cmdline_path = pid_str / "cmdline"
        try:
            cmdline = cmdline_path.read_bytes().decode("utf-8", errors="replace")
        except (OSError, PermissionError):
            continue
        if "agentalloy.app" in cmdline:
            try:
                pids.append(int(pid_str.name))
            except ValueError:
                continue

    assert all(p > 0 for p in pids), "collected PIDs must be positive integers"  # P10-R5
    return min(pids) if pids else None


def _pid_alive(pid: int) -> bool:
    """Return True if ``pid`` is still running."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we can't signal it (e.g. different user).
        return True


def stop_service_in_container(no_restart: bool = False) -> bool:
    """Stop the running uvicorn service in a container.

    Scans ``/proc`` for a uvicorn process, sends SIGTERM, and waits up to
    15 seconds for it to exit. If it does not, escalates to SIGKILL.

    Sentinel reentrancy guard: sets ``AGENTALLOY_DB_LOCK_HELD=1`` in
    ``os.environ`` after confirming a real process exists. Child processes
    spawned via ``subprocess.run()`` inherit this sentinel (POSIX default)
    and short-circuit their own stop attempt — preventing N stop/restart
    cycles when composed commands (install-packs → ingest) each try to
    manage the lifecycle independently. Sentinel lifetime = this call →
    matching ``restart_service_in_container()`` call.

    When ``no_restart`` is True, this function is a no-op (returns False).
    Returns ``True`` if a process was found and stopped, ``False`` otherwise.
    """
    if not isinstance(no_restart, bool):
        raise TypeError(f"no_restart must be bool, got {type(no_restart).__name__}")
    if no_restart:
        return False
    # Sentinel check: if an ancestor process already owns the lifecycle, no-op.
    # POSIX subprocess.run() inherits os.environ — child ingest processes see this.
    if os.environ.get("AGENTALLOY_DB_LOCK_HELD"):
        return False  # ancestor owns stop/restart lifecycle

    pid = _find_uvicorn_pid()
    if pid is None:
        return False  # D1: no service running — sentinel NOT set (nothing was stopped)

    # Set sentinel AFTER confirming a real stop is happening.
    # POSIX-global lifetime: this stop → matching restart_service_in_container() call.
    os.environ["AGENTALLOY_DB_LOCK_HELD"] = "1"
    assert os.environ.get("AGENTALLOY_DB_LOCK_HELD") == "1"  # P10-R5: sentinel confirmed

    # SIGTERM first — graceful shutdown.
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        # Process already exited between scan and kill.
        os.environ.pop("AGENTALLOY_DB_LOCK_HELD", None)
        return True
    except PermissionError:
        # Cannot signal — pop sentinel since no stop occurred.
        os.environ.pop("AGENTALLOY_DB_LOCK_HELD", None)
        return False

    # Poll for exit up to 15 seconds.
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.2)

    # Escalate to SIGKILL.
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except PermissionError:
        # Cannot signal — pop sentinel since no stop occurred.
        os.environ.pop("AGENTALLOY_DB_LOCK_HELD", None)
        return False

    # Brief wait for kernel to reap.
    time.sleep(0.4)
    if _pid_alive(pid):
        # Cannot signal — pop sentinel since no stop occurred.
        os.environ.pop("AGENTALLOY_DB_LOCK_HELD", None)
        return False
    return True


def restart_service_in_container(no_restart: bool = False) -> bool:
    """Restart the uvicorn service inside a container.

    Reads the configured port from install state, constructs the uvicorn
    command, spawns it as a background subprocess, then polls the
    ``/health`` endpoint for up to 30 seconds.

    Clears the ``AGENTALLOY_DB_LOCK_HELD`` sentinel BEFORE copying
    ``os.environ`` for the child process — if cleared after, the spawned
    uvicorn would inherit the sentinel and all future in-process stops
    would silently no-op forever.

    When ``no_restart`` is True, this function is a no-op (returns True).
    Returns ``True`` if the service became healthy (or no-op), ``False`` otherwise.
    """
    if not isinstance(no_restart, bool):
        raise TypeError(f"no_restart must be bool, got {type(no_restart).__name__}")
    if no_restart:
        return True
    # T6: clear sentinel BEFORE env copy — spawned uvicorn must not inherit it.
    # P10-R7: pop() with default avoids KeyError on non-owned restarts.
    os.environ.pop("AGENTALLOY_DB_LOCK_HELD", None)
    assert "AGENTALLOY_DB_LOCK_HELD" not in os.environ  # P10-R5: sentinel cleared

    # Build the uvicorn command from state.
    st = install_state.load_state()
    port = install_state.validate_port(st.get("port", 47950))
    cmd = f"uv run uvicorn agentalloy.app:app --host 0.0.0.0 --port {port}"

    log_path = server_proc.server_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Parse the command into a list for subprocess.Popen.
    cmd_list = cmd.split()

    # Load the user's .env file so the restarted service has the same
    # runtime configuration as the original (API keys, model settings, etc.).
    env = os.environ.copy()
    env_path = install_state.user_data_dir() / ".env"
    if env_path.is_file():
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()
        except OSError:
            pass

    proc: subprocess.Popen[bytes] | None = None
    started = False
    try:
        with open(log_path, "ab", buffering=0) as log_fh:
            proc = subprocess.Popen(
                cmd_list,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                env=env,
            )
        started = True
    except Exception:
        return False

    # Poll /health endpoint up to 30 seconds.
    # The `finally` block guarantees cleanup (terminate + kill) so the child
    # process is never orphaned when the timeout fires.
    deadline = time.monotonic() + 30.0
    try:
        while time.monotonic() < deadline:
            if server_proc.port_reachable(port):
                # Verify the process is still alive (not crashed immediately).
                if proc.poll() is not None:
                    # Process died — fall through to cleanup.
                    break
                return True
            time.sleep(0.5)
    finally:
        # Always clean up the child process to prevent orphans.
        if started:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001 — includes TimeoutExpired; force-kill below
                with contextlib.suppress(OSError):
                    proc.kill()
    return False


def test_corpus_lock_released() -> bool:
    """Test whether the corpus DB (agentalloy.duck) write-lock is released.

    Prefers ``DUCKDB_PATH`` env var (set by the container run env to
    ``/app/data/agentalloy.duck``) over ``corpus_dir()`` — the latter
    resolves to the host home directory and silently skips the check
    inside a container where the volume-mounted DB lives elsewhere.

    Opens a read-only skill-store connection, then closes it in a
    ``finally`` block so the file lock is released before the caller
    opens the real skill-store connection.

    Retries up to 5 seconds at 0.5-second intervals.
    Returns ``True`` if the lock is released, ``False`` if still locked.
    """
    # T3: prefer DUCKDB_PATH env (container run env sets /app/data/agentalloy.duck)
    env_path = os.environ.get("DUCKDB_PATH")
    if env_path is not None:
        skills_path = Path(env_path)
    else:
        skills_path = install_state.corpus_dir() / "agentalloy.duck"

    assert skills_path is not None, "skills_path must resolve to a non-None Path"  # P10-R5

    from agentalloy.storage.skill_store import DuckDBSkillStore, open_skill_store

    max_retries = 10  # P10-R2: 10 iterations × 0.5s = 5s max wait
    retry_interval = 0.5

    for attempt in range(max_retries):  # P10-R2: bounded = max_retries = 10
        store: DuckDBSkillStore | None = None
        try:
            # A read-only open succeeds only when no writer holds the lock.
            store = open_skill_store(str(skills_path), read_only=True)
            # Success — lock is released.
            return True
        except Exception:
            if attempt < max_retries - 1:
                time.sleep(retry_interval)
        finally:
            # T4: explicitly release file handle before caller opens real connection.
            if store is not None:
                store.close()  # P10-R7: drops the read-only handle → lock release

    return False
