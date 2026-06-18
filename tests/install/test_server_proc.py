# pyright: reportPrivateUsage=false, reportUnknownLambdaType=false, reportUnknownArgumentType=false
"""Tests for server-lifecycle helpers and CLI verbs.

Process-management code is awkward to unit-test, so we cover:

* ``find_listening_pid`` against mocked ``ss`` output (parsing).
* ``stop`` against a real short-lived child process (signal + wait loop).
* The four ``server-*`` subcommands at the dispatcher level (registration).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Any
from unittest.mock import patch

import pytest

from agentalloy.install import server_proc
from agentalloy.install.__main__ import build_parser
from agentalloy.install.subcommands import server_stop

# ---------------------------------------------------------------------------
# find_listening_pid — output-parsing
# ---------------------------------------------------------------------------


def _ss_result(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["ss"], returncode=returncode, stdout=stdout, stderr="")


class TestFindListeningPid:
    def test_extracts_pid_from_typical_ss_line(self) -> None:
        stdout = 'LISTEN 0 2048 127.0.0.1:47950 0.0.0.0:* users:(("python",pid=1234,fd=5))\n'
        with patch("subprocess.run", return_value=_ss_result(stdout)):
            assert server_proc.find_listening_pid(47950) == 1234

    def test_returns_none_when_ss_finds_nothing(self) -> None:
        with patch("subprocess.run", return_value=_ss_result("")):
            assert server_proc.find_listening_pid(47950) is None

    def test_returns_none_when_ss_errors(self) -> None:
        with patch("subprocess.run", return_value=_ss_result("", returncode=1)):
            assert server_proc.find_listening_pid(47950) is None

    def test_returns_none_when_ss_missing(self) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert server_proc.find_listening_pid(47950) is None

    def test_returns_none_on_ss_timeout(self) -> None:
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="ss", timeout=2.0),
        ):
            assert server_proc.find_listening_pid(47950) is None

    def test_handles_wildcard_bind(self) -> None:
        stdout = 'LISTEN 0 2048 *:47950 *:* users:(("python",pid=42,fd=5))\n'
        with patch("subprocess.run", return_value=_ss_result(stdout)):
            assert server_proc.find_listening_pid(47950) == 42

    def test_picks_first_pid_when_multiple_lines(self) -> None:
        # ss can emit multiple entries (e.g. uvicorn worker + reloader);
        # we take the first match. Distinct PIDs prove the order matters.
        stdout = (
            "LISTEN 0 2048 127.0.0.1:47950 0.0.0.0:* "
            'users:(("python",pid=100,fd=5))\n'
            "LISTEN 0 2048 127.0.0.1:47950 0.0.0.0:* "
            'users:(("python",pid=101,fd=6))\n'
        )
        with patch("subprocess.run", return_value=_ss_result(stdout)):
            assert server_proc.find_listening_pid(47950) == 100


# ---------------------------------------------------------------------------
# stop — real child-process signaling
# ---------------------------------------------------------------------------


@pytest.fixture()
def long_lived_child() -> Any:
    """Spawn a child that exits cleanly on SIGTERM. ``sleep`` is the simplest
    such process; Python's default-SIGTERM behavior racing with import-time
    setup made this flaky earlier."""
    proc = subprocess.Popen(
        ["sleep", "30"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Tiny delay so the child is actually in the sleep syscall before we
    # signal it; otherwise on a slow CI box the signal can land before
    # exec is complete.
    time.sleep(0.1)
    yield proc.pid
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=2)


class TestStop:
    def test_sigterm_stops_responsive_process(self, long_lived_child: int) -> None:
        outcome = server_proc.stop(long_lived_child, timeout_s=5.0)
        assert outcome == "term"
        assert not server_proc._pid_alive(long_lived_child)

    def test_raises_for_unknown_pid(self) -> None:
        # PID 999999 is almost certainly not allocated; if it is, the test
        # is racy but the kernel will still raise ProcessLookupError on
        # signal to a non-running pid.
        with pytest.raises(server_proc.ServerLifecycleError):
            server_proc.stop(999_999, timeout_s=1.0)

    def test_sigkill_escalation_on_unresponsive_process(self) -> None:
        # Spawn a child that ignores SIGTERM. timeout_s is short so we
        # don't make the test sluggish; SIGKILL is unblockable.
        script = (
            "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(30)\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            # Give the child a moment to install the signal handler.
            time.sleep(0.3)
            outcome = server_proc.stop(proc.pid, timeout_s=0.5)
            assert outcome == "kill"
            # Reap the zombie so pytest doesn't warn.
            proc.wait(timeout=2)
            assert not server_proc._pid_alive(proc.pid)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2)


# ---------------------------------------------------------------------------
# Pid-alive predicate
# ---------------------------------------------------------------------------


class TestPidAlive:
    def test_self_is_alive(self) -> None:
        assert server_proc._pid_alive(os.getpid()) is True

    def test_unallocated_pid_is_not_alive(self) -> None:
        assert server_proc._pid_alive(999_999) is False


# ---------------------------------------------------------------------------
# .env loading parity with serve
# ---------------------------------------------------------------------------


class TestStartBackgroundEnvLoading:
    """``start_background`` must produce the same child env as ``serve``."""

    def test_parses_env_file_into_child_env(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text('# comment line\nFOO=bar\nexport QUOTED="hello world"\nBLANK=\n')
        monkeypatch.setattr("agentalloy.install.state.env_path", lambda: env_file)
        # Ensure these aren't already in os.environ (so they get picked up).
        monkeypatch.delenv("FOO", raising=False)
        monkeypatch.delenv("QUOTED", raising=False)

        captured: dict[str, Any] = {}

        class _FakePopen:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                captured["env"] = kwargs.get("env")
                self.pid = 1234

        monkeypatch.setattr("subprocess.Popen", _FakePopen)
        # Pretend nothing is listening so start_background proceeds.
        monkeypatch.setattr(server_proc, "find_listening_pid", lambda *a, **k: None)

        server_proc.start_background(47999)

        child_env = captured["env"]
        assert child_env["FOO"] == "bar"
        assert child_env["QUOTED"] == "hello world"
        assert child_env["BLANK"] == ""

    def test_process_env_takes_precedence_over_env_file(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("FOO=from_file\n")
        monkeypatch.setattr("agentalloy.install.state.env_path", lambda: env_file)
        monkeypatch.setenv("FOO", "from_process")

        captured: dict[str, Any] = {}

        class _FakePopen:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                captured["env"] = kwargs.get("env")
                self.pid = 1234

        monkeypatch.setattr("subprocess.Popen", _FakePopen)
        monkeypatch.setattr(server_proc, "find_listening_pid", lambda *a, **k: None)

        server_proc.start_background(47999)

        assert captured["env"]["FOO"] == "from_process"


# ---------------------------------------------------------------------------
# Dispatcher registration
# ---------------------------------------------------------------------------


class TestDispatcherRegistration:
    @pytest.mark.parametrize(
        "verb",
        ["server-status", "server-start", "server-stop", "server-restart"],
    )
    def test_verb_is_registered(self, verb: str) -> None:
        parser = build_parser()
        args = parser.parse_args([verb])
        assert args.subcommand == verb
        assert callable(args.func)


class TestServerStopAlreadyStopped:
    """`server-stop` against an idle port is success, not EXIT_NOOP.

    Stopping an already-stopped service is the desired post-condition;
    scripts that care can read `action: "already_stopped"` from JSON.
    """

    def test_returns_zero_with_already_stopped(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch.object(server_proc, "find_listening_pid", return_value=None),
            patch.object(server_proc, "configured_port", return_value=47950),
        ):
            args = argparse.Namespace(port=None, timeout=10.0, json=True)
            rc = server_stop._run(args)
        captured = capsys.readouterr()
        assert rc == 0
        payload = json.loads(captured.out)
        assert payload["action"] == "already_stopped"
        assert payload["port"] == 47950


# ---------------------------------------------------------------------------
# reclaim_stale_port — only kills a holder whose /proc cmdline matches ours
# ---------------------------------------------------------------------------


class TestReclaimStalePort:
    _EMBED_CMD = "/usr/bin/llama-server --embeddings --pooling mean --port 47951 -m /x/nomic-embed-text-v1.5.Q8_0.gguf"

    def test_kills_on_matching_signature(self) -> None:
        with (
            patch("agentalloy.install.server_proc.find_listening_pid", return_value=999),
            patch("agentalloy.install.server_proc._read_cmdline", return_value=self._EMBED_CMD),
            patch("agentalloy.install.server_proc.stop", return_value="term") as mock_stop,
        ):
            pid = server_proc.reclaim_stale_port(47951, ["llama-server", "nomic-embed"])
        assert pid == 999
        mock_stop.assert_called_once_with(999)

    def test_spares_foreign_holder(self) -> None:
        with (
            patch("agentalloy.install.server_proc.find_listening_pid", return_value=999),
            patch(
                "agentalloy.install.server_proc._read_cmdline",
                return_value="/usr/bin/some-unrelated-server --port 47951",
            ),
            patch("agentalloy.install.server_proc.stop") as mock_stop,
        ):
            pid = server_proc.reclaim_stale_port(47951, ["llama-server", "nomic-embed"])
        assert pid is None
        mock_stop.assert_not_called()

    def test_partial_match_does_not_kill(self) -> None:
        # cmdline has llama-server but the WRONG model — not our embed server.
        with (
            patch("agentalloy.install.server_proc.find_listening_pid", return_value=999),
            patch(
                "agentalloy.install.server_proc._read_cmdline",
                return_value="/usr/bin/llama-server --port 47951 -m /x/some-other-model.gguf",
            ),
            patch("agentalloy.install.server_proc.stop") as mock_stop,
        ):
            assert server_proc.reclaim_stale_port(47951, ["llama-server", "nomic-embed"]) is None
        mock_stop.assert_not_called()

    def test_no_holder_returns_none(self) -> None:
        with (
            patch("agentalloy.install.server_proc.find_listening_pid", return_value=None),
            patch("agentalloy.install.server_proc.stop") as mock_stop,
        ):
            assert server_proc.reclaim_stale_port(47951, ["llama-server"]) is None
        mock_stop.assert_not_called()

    def test_empty_match_never_kills(self) -> None:
        with (
            patch("agentalloy.install.server_proc.find_listening_pid", return_value=999),
            patch("agentalloy.install.server_proc.stop") as mock_stop,
        ):
            assert server_proc.reclaim_stale_port(47951, []) is None
        mock_stop.assert_not_called()
