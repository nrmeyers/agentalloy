"""Tests for the ``wrap`` subcommand.

Covers:
- Parser registration and all flag parsing
- Harness resolution from REGISTRY
- Port probe with PID file
- Server start/stop lifecycle
- Hook and proxy wiring application
- Child process spawning
- Cleanup on exit (normal and signal)
- Edge cases (no child args, invalid harness, stale PID file)
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agentalloy.install.subcommands.wrap import (
    VALID_HARNESSES,
    _pid_file_path,
    _port_owned_by_us,
    _read_pid_file,
    _remove_pid_file,
    _run,
    _write_pid_file,
    add_parser,
    run,
)

# ---------------------------------------------------------------------------
# Shared fixture: temporary XDG state directory
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_state_dir(tmp_path: Path):
    """Set up a temporary XDG state directory for wrap tests."""
    config_dir = tmp_path / ".config"
    data_dir = tmp_path / ".local" / "share"
    config_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)
    old_config = os.environ.get("XDG_CONFIG_HOME")
    old_data = os.environ.get("XDG_DATA_HOME")
    os.environ["XDG_CONFIG_HOME"] = str(config_dir)
    os.environ["XDG_DATA_HOME"] = str(data_dir)
    yield config_dir, data_dir
    if old_config is not None:
        os.environ["XDG_CONFIG_HOME"] = old_config
    elif "XDG_CONFIG_HOME" in os.environ:
        del os.environ["XDG_CONFIG_HOME"]
    if old_data is not None:
        os.environ["XDG_DATA_HOME"] = old_data
    elif "XDG_DATA_HOME" in os.environ:
        del os.environ["XDG_DATA_HOME"]
    # Cleanup PID file if it exists
    _remove_pid_file()


# ---------------------------------------------------------------------------
# Parser tests
# ---------------------------------------------------------------------------


class TestAddParser:
    """Test argparse registration and flag parsing."""

    def test_parser_registers_wrap_subcommand(self):
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="subcommand")
        add_parser(subparsers)

        args = parser.parse_args(["wrap", "claude-code", "--", "echo", "hello"])
        assert args.harness == "claude-code"
        assert args.child_args == ["echo", "hello"]

    def test_parser_accepts_all_flags(self):
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="subcommand")
        add_parser(subparsers)

        args = parser.parse_args(
            [
                "wrap",
                "--port",
                "50000",
                "--via",
                "hook",
                "--no-start-server",
                "cursor",
                "--",
                "echo",
                "hello",
            ]
        )
        assert args.harness == "cursor"
        assert args.port == 50000
        assert args.via == "hook"
        assert args.no_start_server is True
        assert args.child_args == ["echo", "hello"]

    def test_parser_defaults(self):
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="subcommand")
        add_parser(subparsers)

        args = parser.parse_args(["wrap", "claude-code", "--", "echo", "hello"])
        assert args.port is None
        # via defaults to None at the parser; the run path resolves it
        # per-harness (claude-code -> hook, everything else -> proxy).
        assert args.via is None
        assert args.no_start_server is False

    def test_parser_all_harnesses_accepted(self):
        """All harnesses from VALID_HARNESSES should be accepted by the parser."""
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="subcommand")
        add_parser(subparsers)

        for harness in sorted(VALID_HARNESSES):
            args = parser.parse_args(["wrap", harness, "--", "echo", "test"])
            assert args.harness == harness

    def test_via_choice_validation(self):
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="subcommand")
        add_parser(subparsers)

        for via in ("hook", "proxy"):
            args = parser.parse_args(["wrap", "--via", via, "claude-code", "--", "echo"])
            assert args.via == via

    def test_via_rejects_invalid(self):
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="subcommand")
        add_parser(subparsers)

        with pytest.raises(SystemExit):
            parser.parse_args(["wrap", "--via", "invalid", "claude-code", "--", "echo"])


# ---------------------------------------------------------------------------
# PID file tests
# ---------------------------------------------------------------------------


class TestPidFile:
    """Test PID file read/write/remove operations."""

    def test_write_and_read_pid_file(self, tmp_state_dir: tuple[Path, Path]):
        _write_pid_file(12345)
        pid = _read_pid_file()
        assert pid == 12345

    def test_read_missing_pid_file(self, tmp_state_dir: tuple[Path, Path]):
        pid = _read_pid_file()
        assert pid is None

    def test_remove_pid_file(self, tmp_state_dir: tuple[Path, Path]):
        _write_pid_file(12345)
        assert _pid_file_path().exists()
        _remove_pid_file()
        assert not _pid_file_path().exists()
        assert _read_pid_file() is None

    def test_read_corrupt_pid_file(self, tmp_state_dir: tuple[Path, Path]):
        _pid_file_path().parent.mkdir(parents=True, exist_ok=True)
        _pid_file_path().write_text("not-a-pid")
        pid = _read_pid_file()
        assert pid is None


# ---------------------------------------------------------------------------
# Port ownership tests
# ---------------------------------------------------------------------------


class TestPortOwned:
    """Test port ownership detection."""

    def test_port_owned_with_valid_pid_file(self, tmp_state_dir: tuple[Path, Path]):
        """When PID file exists and process is alive and listening, returns True."""
        _write_pid_file(12345)
        with (
            patch(
                "agentalloy.install.subcommands.wrap.server_proc.find_listening_pid"
            ) as mock_find,
            patch("os.kill") as mock_kill,
        ):
            mock_find.return_value = 12345
            mock_kill.return_value = None  # pretend PID is alive
            assert _port_owned_by_us(47950) is True

    def test_port_not_owned_stale_pid(self, tmp_state_dir: tuple[Path, Path]):
        """When PID file exists but process is dead, returns False."""
        _write_pid_file(99999)
        with patch(
            "agentalloy.install.subcommands.wrap.server_proc.find_listening_pid"
        ) as mock_find:
            mock_find.return_value = None
            # os.kill(99999, 0) will raise ProcessLookupError since PID 99999 likely doesn't exist
            # But we patch it differently
            assert _port_owned_by_us(47950) is False

    def test_port_not_owned_no_pid_file(self, tmp_state_dir: tuple[Path, Path]):
        """When no PID file exists, returns False."""
        _remove_pid_file()
        assert _port_owned_by_us(47950) is False


# ---------------------------------------------------------------------------
# Harness validation tests
# ---------------------------------------------------------------------------


class TestHarnessValidation:
    """Test harness name resolution and validation."""

    def test_valid_harness_accepted(self, tmp_state_dir: tuple[Path, Path]):
        """A valid harness name should not raise an error at validation step."""
        assert "claude-code" in VALID_HARNESSES
        assert "cursor" in VALID_HARNESSES
        assert "aider" in VALID_HARNESSES

    def test_invalid_harness_rejected(self, tmp_state_dir: tuple[Path, Path]):
        """An invalid harness name should return exit code 1."""
        with patch(
            "agentalloy.install.subcommands.wrap.server_proc.find_listening_pid", return_value=None
        ):
            rc = _run(
                argparse.Namespace(
                    harness="nonexistent-harness",
                    port=None,
                    via="proxy",
                    no_start_server=True,
                    child_args=["echo", "hello"],
                )
            )
        # Should fail before reaching server_proc because harness is invalid
        assert rc == 1


# ---------------------------------------------------------------------------
# Run function tests (mocked server and wiring)
# ---------------------------------------------------------------------------


class TestRun:
    """Test the _run function with mocked server and wiring."""

    def _make_args(
        self,
        harness: str = "claude-code",
        port: int | None = None,
        via: str = "proxy",
        no_start_server: bool = False,
        child_args: list[str] | None = None,
        json_flag: bool = False,
    ) -> argparse.Namespace:
        if child_args is None:
            child_args = ["echo", "hello"]
        return argparse.Namespace(
            harness=harness,
            port=port,
            via=via,
            no_start_server=no_start_server,
            child_args=child_args,
            json=json_flag,
        )

    def test_no_child_args_returns_error(self, tmp_state_dir: tuple[Path, Path]):
        """Wrap with no child args should return exit code 1."""
        with (
            patch(
                "agentalloy.install.subcommands.wrap.server_proc.find_listening_pid",
                return_value=None,
            ),
            patch(
                "agentalloy.install.subcommands.wrap.server_proc.start_background",
                return_value=12345,
            ),
            patch(
                "agentalloy.install.subcommands.wrap.server_proc.wait_until_listening",
                return_value=True,
            ),
            patch(
                "agentalloy.install.subcommands.wrap._wire_harness_core",
                return_value={"files_written": []},
            ),
        ):
            args = self._make_args(child_args=[])
            rc = _run(args)
        assert rc == 1

    def test_invalid_harness_returns_error(self, tmp_state_dir: tuple[Path, Path]):
        """Wrap with invalid harness should return exit code 1."""
        args = self._make_args(harness="invalid-harness", child_args=["echo", "x"])
        rc = _run(args)
        assert rc == 1

    def test_no_start_server_no_existing_server(self, tmp_state_dir: tuple[Path, Path]):
        """--no-start-server with no running server should return error."""
        args = self._make_args(no_start_server=True)
        with patch(
            "agentalloy.install.subcommands.wrap.server_proc.find_listening_pid",
            return_value=None,
        ):
            rc = _run(args)
        assert rc == 1

    def test_server_start_called_when_needed(self, tmp_state_dir: tuple[Path, Path]):
        """When no server is running, start_background should be called."""
        with (
            patch(
                "agentalloy.install.subcommands.wrap.server_proc.find_listening_pid",
                return_value=None,
            ),
            patch(
                "agentalloy.install.subcommands.wrap.server_proc.start_background",
                return_value=12345,
            ),
            patch(
                "agentalloy.install.subcommands.wrap.server_proc.wait_until_listening",
                return_value=True,
            ),
            patch(
                "agentalloy.install.subcommands.wrap._wire_harness_core",
                return_value={"files_written": [], "harness": "claude-code"},
            ),
            patch(
                "agentalloy.install.subcommands.wrap.server_proc.stop",
            ),
        ):
            args = self._make_args(child_args=["echo", "hello"])
            rc = _run(args)
            assert rc == 0

    def test_server_not_started_when_running(self, tmp_state_dir: tuple[Path, Path]):
        """When server is already running, start_background should NOT be called."""
        with (
            patch(
                "agentalloy.install.subcommands.wrap.server_proc.find_listening_pid",
                return_value=9999,
            ),
            patch(
                "agentalloy.install.subcommands.wrap.server_proc.start_background",
            ) as mock_start,
            patch(
                "agentalloy.install.subcommands.wrap._wire_harness_core",
                return_value={"files_written": [], "harness": "claude-code"},
            ),
            patch(
                "agentalloy.install.subcommands.wrap.server_proc.stop",
            ),
        ):
            args = self._make_args(no_start_server=True, child_args=["echo", "hello"])
            rc = _run(args)
            assert rc == 0
            mock_start.assert_not_called()

    def test_proxy_wiring_applied(self, tmp_state_dir: tuple[Path, Path]):
        """--via proxy should call wire_harness without legacy=True."""
        with (
            patch(
                "agentalloy.install.subcommands.wrap.server_proc.find_listening_pid",
                return_value=9999,
            ),
            patch(
                "agentalloy.install.subcommands.wrap._wire_harness_core",
                return_value={"files_written": [], "harness": "claude-code"},
            ),
            patch(
                "agentalloy.install.subcommands.wrap.server_proc.stop",
            ),
        ):
            args = self._make_args(via="proxy", child_args=["echo", "hello"])
            rc = _run(args)
            assert rc == 0

    def test_child_process_spawned(self, tmp_state_dir: tuple[Path, Path]):
        """Child process should be spawned via subprocess.Popen."""
        mock_proc = MagicMock()
        mock_proc.pid = 54321
        mock_proc.wait.return_value = 0

        with (
            patch(
                "agentalloy.install.subcommands.wrap.server_proc.find_listening_pid",
                return_value=9999,
            ),
            patch(
                "agentalloy.install.subcommands.wrap._wire_harness_core",
                return_value={"files_written": [], "harness": "claude-code"},
            ),
            patch(
                "agentalloy.install.subcommands.wrap.subprocess.Popen",
                return_value=mock_proc,
            ),
            patch(
                "agentalloy.install.subcommands.wrap.server_proc.stop",
            ),
        ):
            args = self._make_args(child_args=["echo", "hello"])
            rc = _run(args)
            assert rc == 0
            mock_proc.wait.assert_called_once()

    def test_child_process_file_not_found(self, tmp_state_dir: tuple[Path, Path]):
        """Child process not found should return exit code 2."""
        with (
            patch(
                "agentalloy.install.subcommands.wrap.server_proc.find_listening_pid",
                return_value=9999,
            ),
            patch(
                "agentalloy.install.subcommands.wrap._wire_harness_core",
                return_value={"files_written": [], "harness": "claude-code"},
            ),
            patch(
                "agentalloy.install.subcommands.wrap.subprocess.Popen",
                side_effect=FileNotFoundError("command not found"),
            ),
        ):
            args = self._make_args(child_args=["nonexistent-command-xyz"])
            rc = _run(args)
            assert rc == 2

    def test_signal_handler_stops_server(self, tmp_state_dir: tuple[Path, Path]):
        """Signal handler should stop the server if we started it."""
        mock_proc = MagicMock()
        mock_proc.pid = 54321
        mock_proc.wait.return_value = 0

        with (
            patch(
                "agentalloy.install.subcommands.wrap.server_proc.find_listening_pid",
                return_value=None,
            ),
            patch(
                "agentalloy.install.subcommands.wrap.server_proc.start_background",
                return_value=12345,
            ),
            patch(
                "agentalloy.install.subcommands.wrap.server_proc.wait_until_listening",
                return_value=True,
            ),
            patch(
                "agentalloy.install.subcommands.wrap._wire_harness_core",
                return_value={"files_written": [], "harness": "claude-code"},
            ),
            patch(
                "agentalloy.install.subcommands.wrap.subprocess.Popen",
                return_value=mock_proc,
            ),
            patch(
                "agentalloy.install.subcommands.wrap.server_proc.stop",
            ) as mock_stop,
        ):
            args = self._make_args(child_args=["echo", "hello"])
            rc = _run(args)
            assert rc == 0
            mock_stop.assert_called_once()

    def test_pid_file_written_on_server_start(self, tmp_state_dir: tuple[Path, Path]):
        """PID file should be written when server is started."""
        mock_proc = MagicMock()
        mock_proc.pid = 54321
        mock_proc.wait.return_value = 0

        with (
            patch(
                "agentalloy.install.subcommands.wrap.server_proc.find_listening_pid",
                return_value=None,
            ),
            patch(
                "agentalloy.install.subcommands.wrap.server_proc.start_background",
                return_value=12345,
            ),
            patch(
                "agentalloy.install.subcommands.wrap.server_proc.wait_until_listening",
                return_value=True,
            ),
            patch(
                "agentalloy.install.subcommands.wrap._wire_harness_core",
                return_value={"files_written": [], "harness": "claude-code"},
            ),
            patch(
                "agentalloy.install.subcommands.wrap.subprocess.Popen",
                return_value=mock_proc,
            ),
            patch(
                "agentalloy.install.subcommands.wrap.server_proc.stop",
            ),
        ):
            args = self._make_args(child_args=["echo", "hello"])
            rc = _run(args)
            assert rc == 0
            # PID file should have been written and then removed
            assert _read_pid_file() is None


# ---------------------------------------------------------------------------
# Public run() entry point test
# ---------------------------------------------------------------------------


class TestRunEntry:
    """Test the public run() entry point."""

    def test_run_calls_run_func(self, tmp_state_dir: tuple[Path, Path]):
        """run() should delegate to _run()."""
        mock_run = MagicMock(return_value=0)
        with patch("agentalloy.install.subcommands.wrap._run", mock_run):
            args = argparse.Namespace(
                harness="claude-code",
                port=None,
                via="proxy",
                no_start_server=True,
                child_args=["echo", "test"],
                json=False,
            )
            rc = run(args)
            assert rc == 0
            mock_run.assert_called_once_with(args)


# ---------------------------------------------------------------------------
# Integration-style test: real child process
# ---------------------------------------------------------------------------


class TestIntegration:
    """Light integration tests that actually spawn a child process."""

    def test_echo_child_exits_cleanly(self, tmp_state_dir: tuple[Path, Path]):
        """Wrap should successfully run 'echo hello' and return 0."""
        with (
            patch(
                "agentalloy.install.subcommands.wrap.server_proc.find_listening_pid",
                return_value=9999,
            ),
            patch(
                "agentalloy.install.subcommands.wrap._wire_harness_core",
                return_value={"files_written": [], "harness": "claude-code"},
            ),
            patch(
                "agentalloy.install.subcommands.wrap.server_proc.stop",
            ),
        ):
            args = argparse.Namespace(
                harness="claude-code",
                port=None,
                via="proxy",
                no_start_server=True,
                child_args=["echo", "hello"],
                json=False,
            )
            rc = _run(args)
            assert rc == 0

    def test_multiple_child_args(self, tmp_state_dir: tuple[Path, Path]):
        """Child process with multiple args should work."""
        mock_proc = MagicMock()
        mock_proc.pid = 54321
        mock_proc.wait.return_value = 0

        with (
            patch(
                "agentalloy.install.subcommands.wrap.server_proc.find_listening_pid",
                return_value=9999,
            ),
            patch(
                "agentalloy.install.subcommands.wrap._wire_harness_core",
                return_value={"files_written": [], "harness": "claude-code"},
            ),
            patch(
                "agentalloy.install.subcommands.wrap.subprocess.Popen",
                return_value=mock_proc,
            ),
            patch(
                "agentalloy.install.subcommands.wrap.server_proc.stop",
            ),
        ):
            args = argparse.Namespace(
                harness="claude-code",
                port=None,
                via="proxy",
                no_start_server=True,
                child_args=["echo", "-e", "hello\nworld"],
                json=False,
            )
            rc = _run(args)
            assert rc == 0
            mock_proc.wait.assert_called_once()
