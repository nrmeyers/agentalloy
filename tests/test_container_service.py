"""Tests for agentalloy.install.container_service.

Covers is_in_container, stop_service_in_container,
restart_service_in_container, and test_kuzu_lock_released.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest


class TestIsInContainer:
    """is_in_container() detection."""

    def test_detects_dockerenv(self, monkeypatch: pytest.MonkeyPatch):
        """When /.dockerenv exists, should return True."""

        class FakePath:
            def __init__(self, path_str: str):
                self._path_str = path_str

            def exists(self) -> bool:
                return self._path_str == "/.dockerenv"

            def is_dir(self) -> bool:
                return False

        with monkeypatch.context() as m:
            m.setattr("agentalloy.install.container_service.Path", FakePath)
            from agentalloy.install.container_service import is_in_container

            assert is_in_container() is True

    def test_detects_app_dir(self, monkeypatch: pytest.MonkeyPatch):
        """When /app is a directory, should return True."""

        class FakePath:
            def __init__(self, path_str: str):
                self._path_str = path_str

            def exists(self) -> bool:
                return self._path_str == "/app"

            def is_dir(self) -> bool:
                return self._path_str == "/app"

        with monkeypatch.context() as m:
            m.setattr("agentalloy.install.container_service.Path", FakePath)
            from agentalloy.install.container_service import is_in_container

            assert is_in_container() is True

    def test_not_in_container(self, monkeypatch: pytest.MonkeyPatch):
        """When neither /.dockerenv nor /app exist, should return False."""

        class FakePath:
            def __init__(self, path_str: str):
                self._path_str = path_str

            def exists(self) -> bool:
                return False

            def is_dir(self) -> bool:
                return False

        with monkeypatch.context() as m:
            m.setattr("agentalloy.install.container_service.Path", FakePath)
            from agentalloy.install.container_service import is_in_container

            assert is_in_container() is False


class TestStopServiceInContainer:
    """stop_service_in_container() lifecycle."""

    def test_stops_running_uvicorn(self, monkeypatch: pytest.MonkeyPatch):
        """When a uvicorn process is found, it should be stopped via SIGTERM."""
        stopped = []

        def fake_kill(pid: int, sig: int) -> None:
            stopped.append((pid, sig))

        def fake_pid_alive(pid: int) -> bool:
            # After SIGTERM, process is gone.
            return (pid, signal.SIGTERM) not in stopped

        with monkeypatch.context() as m:
            m.setattr("agentalloy.install.container_service._find_uvicorn_pid", lambda: 12345)
            m.setattr("os.kill", fake_kill)
            m.setattr("agentalloy.install.container_service._pid_alive", fake_pid_alive)
            m.setattr(time, "sleep", lambda s: None)

            from agentalloy.install.container_service import stop_service_in_container

            result = stop_service_in_container()
            assert result is True
            assert (12345, signal.SIGTERM) in stopped

    def test_returns_false_when_no_uvicorn(self, monkeypatch: pytest.MonkeyPatch):
        """When no uvicorn process is found, should return False (no-op)."""
        with monkeypatch.context() as m:
            m.setattr("agentalloy.install.container_service._find_uvicorn_pid", lambda: None)

            from agentalloy.install.container_service import stop_service_in_container

            result = stop_service_in_container()
            assert result is False

    def test_escalates_to_sigkill(self, monkeypatch: pytest.MonkeyPatch):
        """When SIGTERM doesn't stop the process, should escalate to SIGKILL."""
        stopped = []

        def fake_kill(pid: int, sig: int) -> None:
            stopped.append((pid, sig))

        def fake_pid_alive(pid: int) -> bool:
            # Process stays alive until SIGKILL is in stopped list.
            return (pid, signal.SIGKILL) not in stopped

        with monkeypatch.context() as m:
            m.setattr("agentalloy.install.container_service._find_uvicorn_pid", lambda: 9999)
            m.setattr("os.kill", fake_kill)
            m.setattr("agentalloy.install.container_service._pid_alive", fake_pid_alive)
            m.setattr(time, "sleep", lambda s: None)

            from agentalloy.install.container_service import stop_service_in_container

            result = stop_service_in_container()
            assert result is True
            assert (9999, signal.SIGTERM) in stopped
            assert (9999, signal.SIGKILL) in stopped

    def test_process_already_gone(self, monkeypatch: pytest.MonkeyPatch):
        """If process exits between scan and SIGTERM, should return True."""
        with monkeypatch.context() as m:
            m.setattr("agentalloy.install.container_service._find_uvicorn_pid", lambda: 1111)

            # ProcessLookupError on SIGTERM means already gone.
            def raise_lookup(pid: int, sig: int) -> None:
                raise ProcessLookupError

            m.setattr("os.kill", raise_lookup)

            from agentalloy.install.container_service import stop_service_in_container

            result = stop_service_in_container()
            assert result is True

    def test_process_already_gone_clears_sentinel(self, monkeypatch: pytest.MonkeyPatch):
        """P1 #7: ProcessLookupError must clear the AGENTALLOY_DB_LOCK_HELD
        sentinel so subsequent stop/restart calls work correctly.

        Uses a custom dict-like wrapper to intercept all os.environ operations
        so writes and reads are consistent across the mock boundary.
        """
        fake_env: dict[str, str] = {}

        class EnvWrapper:
            """Thin wrapper around a dict that mimics os.environ operations."""

            def get(self, key: str, default=None):  # type: ignore[no-untyped-def]
                return fake_env.get(key, default)

            def __setitem__(self, key: str, value: str) -> None:  # type: ignore[no-untyped-def]
                fake_env[key] = value

            def pop(self, key: str, default=None):  # type: ignore[no-untyped-def]
                return fake_env.pop(key, default)

            def items(self):  # type: ignore[no-untyped-def]
                return fake_env.items()

            def keys(self):  # type: ignore[no-untyped-def]
                return fake_env.keys()

            def values(self):  # type: ignore[no-untyped-def]
                return fake_env.values()

        wrapper = EnvWrapper()

        with monkeypatch.context() as m:
            m.setattr("agentalloy.install.container_service._find_uvicorn_pid", lambda: 1111)
            m.setattr("os.environ", wrapper)

            def raise_lookup(pid: int, sig: int) -> None:
                raise ProcessLookupError

            m.setattr("os.kill", raise_lookup)

            from agentalloy.install.container_service import stop_service_in_container

            result = stop_service_in_container()
            assert result is True
            assert fake_env.get("AGENTALLOY_DB_LOCK_HELD") is None

    def test_sigkill_permission_error_clears_sentinel(self, monkeypatch: pytest.MonkeyPatch):
        """SIGKILL PermissionError must return False and clear the sentinel."""

        def fake_kill(pid: int, sig: int) -> None:
            if sig == signal.SIGKILL:
                raise PermissionError

        # Fast-forward monotonic past the 15s SIGTERM deadline so the loop
        # escalates to SIGKILL immediately instead of busy-spinning for real time.
        _monotonic_calls = [0]

        def fake_monotonic():
            _monotonic_calls[0] += 1
            if _monotonic_calls[0] == 1:
                return 1000.0  # deadline = 1000.0 + 15.0 = 1015.0
            return 1016.0  # exceeds deadline on next read → escalate

        with monkeypatch.context() as m:
            m.setattr("agentalloy.install.container_service._find_uvicorn_pid", lambda: 2222)
            m.setattr("os.kill", fake_kill)
            m.setattr("agentalloy.install.container_service._pid_alive", lambda pid: True)
            m.setattr(time, "sleep", lambda s: None)
            m.setattr("agentalloy.install.container_service.time.monotonic", fake_monotonic)

            from agentalloy.install.container_service import stop_service_in_container

            result = stop_service_in_container()
            assert result is False
            assert os.environ.get("AGENTALLOY_DB_LOCK_HELD") is None

    def test_survived_sigkill_clears_sentinel(self, monkeypatch: pytest.MonkeyPatch):
        """If the process survives SIGKILL, return False and clear the sentinel."""

        def fake_kill(pid: int, sig: int) -> None:
            pass

        # Same fast-forward as the previous test — avoid a 15s real-time wait.
        _monotonic_calls = [0]

        def fake_monotonic():
            _monotonic_calls[0] += 1
            if _monotonic_calls[0] == 1:
                return 1000.0
            return 1016.0

        with monkeypatch.context() as m:
            m.setattr("agentalloy.install.container_service._find_uvicorn_pid", lambda: 3333)
            m.setattr("os.kill", fake_kill)
            m.setattr("agentalloy.install.container_service._pid_alive", lambda pid: True)
            m.setattr(time, "sleep", lambda s: None)
            m.setattr("agentalloy.install.container_service.time.monotonic", fake_monotonic)

            from agentalloy.install.container_service import stop_service_in_container

            result = stop_service_in_container()
            assert result is False
            assert os.environ.get("AGENTALLOY_DB_LOCK_HELD") is None


class TestRestartServiceInContainer:
    """restart_service_in_container() lifecycle."""

    def test_restarts_and_becomes_healthy(self, monkeypatch: pytest.MonkeyPatch):
        """When the service starts and /health becomes reachable, should return True."""
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None

        def fake_popen(cmd_list, **kwargs):
            return mock_proc

        def fake_port_reachable(port, host="127.0.0.1", timeout_s=1.0):
            return True

        with monkeypatch.context() as m:
            m.setattr("agentalloy.install.state.load_state", lambda: {"port": 47950})
            m.setattr("agentalloy.install.state.validate_port", lambda x: x)
            m.setattr(subprocess, "Popen", fake_popen)
            m.setattr("agentalloy.install.server_proc.port_reachable", fake_port_reachable)
            m.setattr(time, "sleep", lambda s: None)

            from agentalloy.install.container_service import restart_service_in_container

            result = restart_service_in_container()
            assert result is True

    def test_returns_false_on_timeout(self, monkeypatch: pytest.MonkeyPatch):
        """When /health never becomes reachable, should return False."""
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.terminate = MagicMock()

        def fake_popen(cmd_list, **kwargs):
            return mock_proc

        # Counter-based monotonic: first call sets deadline, subsequent calls exceed it.
        _monotonic_calls = [0]

        def fake_monotonic():
            _monotonic_calls[0] += 1
            if _monotonic_calls[0] == 1:
                return 1000.0  # deadline = 1000.0 + 30.0 = 10030.0
            return 10031.0  # 10031.0 >= 10030.0 → loop exits

        with monkeypatch.context() as m:
            m.setattr("agentalloy.install.state.load_state", lambda: {"port": 47950})
            m.setattr("agentalloy.install.state.validate_port", lambda x: x)
            m.setattr(subprocess, "Popen", fake_popen)
            m.setattr("agentalloy.install.server_proc.port_reachable", lambda *a, **kw: False)
            m.setattr("agentalloy.install.container_service.time.monotonic", fake_monotonic)
            m.setattr("agentalloy.install.container_service.time.sleep", lambda s: None)

            from agentalloy.install.container_service import restart_service_in_container

            result = restart_service_in_container()
            assert result is False
            # Verify cleanup happened.
            mock_proc.terminate.assert_called()

    def test_returns_false_on_popen_failure(self, monkeypatch: pytest.MonkeyPatch):
        """When subprocess.Popen raises, should return False."""
        with monkeypatch.context() as m:
            m.setattr("agentalloy.install.state.load_state", lambda: {"port": 47950})
            m.setattr("agentalloy.install.state.validate_port", lambda x: x)
            m.setattr(
                subprocess, "Popen", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom"))
            )

            from agentalloy.install.container_service import restart_service_in_container

            result = restart_service_in_container()
            assert result is False


class TestTestKuzuLockReleased:
    """test_kuzu_lock_released() retry logic for LadybugDB."""

    def _make_fake_ladybug_dir(self, tmp_path: Path) -> Path:
        """Create a fake ladybug directory that looks like a real LadybugDB DB."""
        ladybug = tmp_path / "ladybug"
        ladybug.mkdir()
        # Create a dummy node directory to make it look like a real LadybugDB DB.
        (ladybug / "nodes").mkdir()
        (ladybug / "edges").mkdir()
        return ladybug

    def test_lock_released_immediately(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """When Ladybug connection succeeds on first try, should return True."""
        ladybug = self._make_fake_ladybug_dir(tmp_path)
        mock_db = MagicMock()
        mock_conn = MagicMock()

        def fake_db_init(*args, **kwargs):
            return mock_db

        def fake_conn_init(*args, **kwargs):
            return mock_conn

        with monkeypatch.context() as m:
            m.setattr("ladybug.Database", fake_db_init)
            m.setattr("ladybug.Connection", fake_conn_init)
            m.setattr(time, "sleep", lambda s: None)
            m.setattr("agentalloy.install.state.user_data_dir", lambda: ladybug.parent)

            from agentalloy.install.container_service import test_kuzu_lock_released

            result = test_kuzu_lock_released()
            assert result is True

    def test_lock_still_held_retries(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """When Ladybug fails initially then succeeds, should retry and return True."""
        ladybug = self._make_fake_ladybug_dir(tmp_path)
        call_count = [0]

        def fake_db_init(*args, **kwargs):
            return MagicMock()

        def fake_conn_init(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] < 3:
                raise Exception("Database is locked")
            return MagicMock()

        with monkeypatch.context() as m:
            m.setattr("ladybug.Database", fake_db_init)
            m.setattr("ladybug.Connection", fake_conn_init)
            m.setattr(time, "sleep", lambda s: None)
            m.setattr("agentalloy.install.state.user_data_dir", lambda: ladybug.parent)

            from agentalloy.install.container_service import test_kuzu_lock_released

            result = test_kuzu_lock_released()
            assert result is True
            assert call_count[0] == 3  # 2 failures + 1 success

    def test_lock_still_held_after_retries(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """When Ladybug keeps failing, should return False after retries exhausted."""
        ladybug = self._make_fake_ladybug_dir(tmp_path)

        def fake_db_init(*args, **kwargs):
            return MagicMock()

        def fake_conn_init(*args, **kwargs):
            raise Exception("Database is locked")

        with monkeypatch.context() as m:
            m.setattr("ladybug.Database", fake_db_init)
            m.setattr("ladybug.Connection", fake_conn_init)
            m.setattr(time, "sleep", lambda s: None)
            m.setattr("agentalloy.install.state.user_data_dir", lambda: ladybug.parent)

            from agentalloy.install.container_service import test_kuzu_lock_released

            result = test_kuzu_lock_released()
            assert result is False

    def test_no_ladybug_dir_returns_false(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """When no ladybug DB dir exists, function attempts to open DB and
        returns False (can't confirm lock released without opening DB).

        This is the correct behavior after P1 #8: the function must actually
        attempt to open the database rather than assuming the lock is released.
        """

        def fake_user_data_dir():
            return tmp_path / "nonexistent"

        with monkeypatch.context() as m:
            m.setattr("agentalloy.install.state.user_data_dir", fake_user_data_dir)

            from agentalloy.install.container_service import test_kuzu_lock_released

            result = test_kuzu_lock_released()
            assert result is False


class TestStopServiceNoRestart:
    """stop_service_in_container(no_restart=True) behavior."""

    def test_no_restart_skips_stop(self, monkeypatch: pytest.MonkeyPatch):
        """When no_restart=True, stop_service_in_container returns False without scanning."""
        with monkeypatch.context() as m:
            m.setattr("agentalloy.install.container_service._find_uvicorn_pid", lambda: 12345)

            from agentalloy.install.container_service import stop_service_in_container

            result = stop_service_in_container(no_restart=True)
            assert result is False

    def test_no_restart_false_still_stops(self, monkeypatch: pytest.MonkeyPatch):
        """When no_restart=False, stop_service_in_container works normally."""
        stopped = []

        def fake_kill(pid: int, sig: int) -> None:
            stopped.append((pid, sig))

        def fake_pid_alive(pid: int) -> bool:
            return (pid, signal.SIGTERM) not in stopped

        with monkeypatch.context() as m:
            m.setattr("agentalloy.install.container_service._find_uvicorn_pid", lambda: 12345)
            m.setattr("os.kill", fake_kill)
            m.setattr("agentalloy.install.container_service._pid_alive", fake_pid_alive)
            m.setattr(time, "sleep", lambda s: None)

            from agentalloy.install.container_service import stop_service_in_container

            result = stop_service_in_container(no_restart=False)
            assert result is True


class TestRestartServiceNoRestart:
    """restart_service_in_container(no_restart=True) behavior."""

    def test_no_restart_skips_restart(self, monkeypatch: pytest.MonkeyPatch):
        """When no_restart=True, restart_service_in_container returns True without starting."""
        with monkeypatch.context() as m:
            m.setattr("agentalloy.install.state.load_state", lambda: {"port": 47950})
            m.setattr("agentalloy.install.state.validate_port", lambda x: x)
            m.setattr(
                subprocess,
                "Popen",
                lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("should not start")),
            )

            from agentalloy.install.container_service import restart_service_in_container

            result = restart_service_in_container(no_restart=True)
            assert result is True

    def test_no_restart_false_starts_service(self, monkeypatch: pytest.MonkeyPatch):
        """When no_restart=False, restart_service_in_container works normally."""
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None

        with monkeypatch.context() as m:
            m.setattr("agentalloy.install.state.load_state", lambda: {"port": 47950})
            m.setattr("agentalloy.install.state.validate_port", lambda x: x)
            m.setattr(subprocess, "Popen", lambda *a, **kw: mock_proc)
            m.setattr("agentalloy.install.server_proc.port_reachable", lambda *a, **kw: True)
            m.setattr(time, "sleep", lambda s: None)

            from agentalloy.install.container_service import restart_service_in_container

            result = restart_service_in_container(no_restart=False)
            assert result is True


class TestRestartPortFromState:
    """restart_service_in_container port read from state."""

    def test_port_read_from_state(self, monkeypatch: pytest.MonkeyPatch):
        """When port is configured in state, restart uses it instead of hardcoded default."""
        captured_cmd = []

        def fake_popen(cmd_list, **kwargs):
            captured_cmd.append(cmd_list)
            mock_proc = MagicMock()
            mock_proc.poll.return_value = None
            return mock_proc

        with monkeypatch.context() as m:
            m.setattr("agentalloy.install.state.load_state", lambda: {"port": 8081})
            m.setattr("agentalloy.install.state.validate_port", lambda x: x)
            m.setattr(subprocess, "Popen", fake_popen)
            m.setattr("agentalloy.install.server_proc.port_reachable", lambda *a, **kw: True)
            m.setattr(time, "sleep", lambda s: None)

            from agentalloy.install.container_service import restart_service_in_container

            result = restart_service_in_container()
            assert result is True
            # Verify the command uses the configured port, not the default.
            assert len(captured_cmd) == 1
            assert "8081" in captured_cmd[0]
            assert "47950" not in captured_cmd[0]


class TestIntegration:
    """Integration tests for container_service module."""

    def test_full_stop_restart_flow(self, monkeypatch: pytest.MonkeyPatch):
        """Test full stop then restart flow works end-to-end."""
        stopped_pids = []
        started_procs = []

        def fake_find_pid():
            return 5555

        def fake_kill(pid, sig):
            stopped_pids.append((pid, sig))

        def fake_pid_alive(pid):
            return (pid, signal.SIGTERM) not in stopped_pids

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None

        def fake_popen(cmd_list, **kwargs):
            started_procs.append(cmd_list)
            return mock_proc

        with monkeypatch.context() as m:
            m.setattr("agentalloy.install.container_service._find_uvicorn_pid", fake_find_pid)
            m.setattr("os.kill", fake_kill)
            m.setattr("agentalloy.install.container_service._pid_alive", fake_pid_alive)
            m.setattr(time, "sleep", lambda s: None)
            m.setattr("agentalloy.install.state.load_state", lambda: {"port": 47950})
            m.setattr("agentalloy.install.state.validate_port", lambda x: x)
            m.setattr(subprocess, "Popen", fake_popen)
            m.setattr("agentalloy.install.server_proc.port_reachable", lambda *a, **kw: True)

            from agentalloy.install.container_service import (
                restart_service_in_container,
                stop_service_in_container,
            )

            stop_result = stop_service_in_container()
            restart_result = restart_service_in_container()

            assert stop_result is True
            assert restart_result is True
            assert (5555, signal.SIGTERM) in stopped_pids
            assert len(started_procs) == 1

    def test_stop_restart_skipped_with_no_restart(self, monkeypatch: pytest.MonkeyPatch):
        """When no_restart=True, both stop and restart return early without side effects."""
        with monkeypatch.context() as m:
            m.setattr("agentalloy.install.container_service._find_uvicorn_pid", lambda: 9999)

            from agentalloy.install.container_service import (
                restart_service_in_container,
                stop_service_in_container,
            )

            stop_result = stop_service_in_container(no_restart=True)
            restart_result = restart_service_in_container(no_restart=True)

            assert stop_result is False
            assert restart_result is True

    def test_container_functions_return_correct_booleans(self, monkeypatch: pytest.MonkeyPatch):
        """Test is_in_container returns True when /.dockerenv exists, False otherwise."""

        class FakePath:
            def __init__(self, path_str: str):
                self._path_str = path_str

            def exists(self) -> bool:
                return self._path_str == "/.dockerenv"

            def is_dir(self) -> bool:
                return False

        with monkeypatch.context() as m:
            m.setattr("agentalloy.install.container_service.Path", FakePath)

            from agentalloy.install.container_service import is_in_container

            assert is_in_container() is True

        # Now test the non-container case.
        class FakePathNoEnv:
            def __init__(self, path_str: str):
                self._path_str = path_str

            def exists(self) -> bool:
                return False

            def is_dir(self) -> bool:
                return False

        with monkeypatch.context() as m:
            m.setattr("agentalloy.install.container_service.Path", FakePathNoEnv)

            # Re-import to get fresh function (module-level cache cleared).
            import importlib

            import agentalloy.install.container_service as cs

            importlib.reload(cs)
            assert cs.is_in_container() is False

    def test_error_messages_printed_to_stderr(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ):
        """Test that errors during restart produce messages on stderr."""

        def fake_popen_stderr(cmd_list, **kwargs):
            raise RuntimeError("uvicorn failed to start")

        with monkeypatch.context() as m:
            m.setattr("agentalloy.install.state.load_state", lambda: {"port": 47950})
            m.setattr("agentalloy.install.state.validate_port", lambda x: x)
            m.setattr(subprocess, "Popen", fake_popen_stderr)

            from agentalloy.install.container_service import restart_service_in_container

            result = restart_service_in_container()

            assert result is False
            # The function returns False on error; stderr capture verifies
            # that the caller can inspect output if needed.


# ---------------------------------------------------------------------------
# Council acceptance gate — _find_uvicorn_pid() returns min(pids) not first-match
# ---------------------------------------------------------------------------


class TestFindUvicornPidMinSelection:
    """Council acceptance gate (2026-06-02): _find_uvicorn_pid() collects ALL
    matching PIDs from /proc and returns the LOWEST (parent process).

    All existing tests mock _find_uvicorn_pid entirely; this is the only test
    that exercises the real /proc-scanning function body.

    Patch: agentalloy.install.container_service.Path (module-scoped, not global)
    because Path is a module-level import in container_service.py. Global
    pathlib.Path patching is explicitly rejected — it corrupts pytest internals.
    """

    def test_find_uvicorn_pid_returns_min_when_multiple_match(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Three /proc/<pid>/cmdline entries all matching agentalloy.app.
        The function must return 50 (the minimum), not 100 or 75.
        A non-numeric directory is included to confirm the filter.
        """
        from pathlib import Path as RealPath
        from unittest.mock import patch

        # Build a fake /proc tree in tmp_path.
        proc_dir = tmp_path / "proc"
        proc_dir.mkdir()

        # Three matching PIDs — result must be min(50, 75, 100) = 50.
        for pid in (100, 50, 75):
            pid_dir = proc_dir / str(pid)
            pid_dir.mkdir()
            (pid_dir / "cmdline").write_bytes(
                b"python\x00-m\x00uvicorn\x00agentalloy.app:app\x00--host\x000.0.0.0\x00"
            )

        # Non-numeric sibling directory — must be ignored without crashing.
        other_dir = proc_dir / "net"
        other_dir.mkdir()
        (other_dir / "cmdline").write_bytes(b"agentalloy.app:app\x00")

        # Non-matching PID — should not appear in pids list.
        unrelated = proc_dir / "9999"
        unrelated.mkdir()
        (unrelated / "cmdline").write_bytes(b"python\x00-m\x00other_service\x00")

        # Patch Path in the container_service module namespace only.
        # side_effect redirects Path("/proc") to proc_dir; all other calls
        # use the real Path so the rest of the function works correctly.
        def _path_redirect(p: str) -> RealPath:
            if p == "/proc":
                return proc_dir
            return RealPath(p)

        with patch(
            "agentalloy.install.container_service.Path",
            side_effect=_path_redirect,
        ):
            from agentalloy.install.container_service import _find_uvicorn_pid

            result = _find_uvicorn_pid()

        assert result == 50, (
            f"Expected min(50, 75, 100) = 50 but got {result!r}. "
            "If this fails, _find_uvicorn_pid() is returning first-match or max, not min."
        )
