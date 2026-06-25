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
import shutil
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


def _cmd_dispatch(**by_tool: Any) -> Any:
    """Build a subprocess.run side_effect that routes by argv[0] (ss vs lsof).

    Each value is either an Exception to raise or a CompletedProcess to return.
    Lets a single patch model "ss is absent but lsof answers" without a real OS.
    """

    def _run(cmd: list[str], *_a: Any, **_k: Any) -> Any:
        spec = by_tool.get(cmd[0])
        if spec is None:
            raise AssertionError(f"unexpected command: {cmd!r}")
        if isinstance(spec, BaseException):
            raise spec
        return spec

    return _run


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

    def test_returns_none_when_ss_and_lsof_missing(self) -> None:
        # ss absent → lsof tried; lsof absent too → None (e.g. a stripped host).
        with patch(
            "subprocess.run",
            side_effect=_cmd_dispatch(ss=FileNotFoundError(), lsof=FileNotFoundError()),
        ):
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

    def test_does_not_call_lsof_when_ss_present(self) -> None:
        # Linux invariant: a working ss must fully resolve the lookup; lsof is
        # never consulted (so Linux behavior is unchanged by the fallback).
        with patch(
            "subprocess.run",
            side_effect=_cmd_dispatch(ss=_ss_result(""), lsof=AssertionError("lsof must not run")),
        ):
            assert server_proc.find_listening_pid(47950) is None


class TestFindListeningPidLsofFallback:
    """ss is Linux-only (iproute2); macOS/BSD fall back to lsof."""

    def test_falls_back_to_lsof_when_ss_absent(self) -> None:
        with patch(
            "subprocess.run",
            side_effect=_cmd_dispatch(
                ss=FileNotFoundError(),
                lsof=subprocess.CompletedProcess(["lsof"], 0, "4242\n", ""),
            ),
        ):
            assert server_proc.find_listening_pid(47950) == 4242

    def test_lsof_no_listener_returns_none(self) -> None:
        with patch(
            "subprocess.run",
            side_effect=_cmd_dispatch(
                ss=FileNotFoundError(),
                lsof=subprocess.CompletedProcess(["lsof"], 1, "", ""),
            ),
        ):
            assert server_proc.find_listening_pid(47950) is None

    def test_lsof_picks_first_pid(self) -> None:
        # lsof -t emits one PID per line (e.g. IPv4 + IPv6 holders).
        with patch(
            "subprocess.run",
            side_effect=_cmd_dispatch(
                ss=FileNotFoundError(),
                lsof=subprocess.CompletedProcess(["lsof"], 0, "100\n101\n", ""),
            ),
        ):
            assert server_proc.find_listening_pid(47950) == 100


# ---------------------------------------------------------------------------
# _read_cmdline — /proc on Linux, ps fallback without /proc (macOS/BSD)
# ---------------------------------------------------------------------------


class TestReadCmdline:
    def test_proc_present_unreadable_pid_returns_empty_no_ps(self) -> None:
        # Linux invariant: when /proc exists but the pid's cmdline is gone, we
        # return "" and never shell out to ps.
        with (
            patch.object(server_proc.Path, "read_bytes", side_effect=FileNotFoundError),
            patch.object(server_proc.Path, "is_dir", return_value=True),
            patch.object(
                server_proc.subprocess, "check_output", side_effect=AssertionError("no ps on Linux")
            ),
        ):
            assert server_proc._read_cmdline(4242) == ""

    def test_falls_back_to_ps_without_proc(self) -> None:
        with (
            patch.object(server_proc.Path, "read_bytes", side_effect=FileNotFoundError),
            patch.object(server_proc.Path, "is_dir", return_value=False),
            patch.object(
                server_proc.subprocess,
                "check_output",
                return_value="uvicorn agentalloy.app --port 47950\n",
            ),
        ):
            assert server_proc._read_cmdline(4242) == "uvicorn agentalloy.app --port 47950"

    def test_ps_cmdline_empty_on_failure(self) -> None:
        with patch.object(server_proc.subprocess, "check_output", side_effect=FileNotFoundError):
            assert server_proc._ps_cmdline(4242) == ""


# ---------------------------------------------------------------------------
# _pid_is_zombie — /proc on Linux, ps fallback without /proc (macOS/BSD)
# ---------------------------------------------------------------------------


class TestPidIsZombie:
    """_pid_is_zombie is the Linux /proc-based zombie check."""

    def test_proc_zombie_state_detected(self) -> None:
        with patch.object(
            server_proc.Path, "read_text", return_value="Name:\tx\nState:\tZ (zombie)\n"
        ):
            assert server_proc._pid_is_zombie(4242) is True

    def test_proc_running_state_not_zombie(self) -> None:
        with patch.object(
            server_proc.Path, "read_text", return_value="Name:\tx\nState:\tS (sleeping)\n"
        ):
            assert server_proc._pid_is_zombie(4242) is False

    def test_unreadable_status_not_zombie(self) -> None:
        # A vanished pid (no /proc/<pid>/status) is not a zombie.
        with patch.object(server_proc.Path, "read_text", side_effect=FileNotFoundError):
            assert server_proc._pid_is_zombie(4242) is False


class TestPsPidAlive:
    """_ps_pid_alive is the macOS/BSD liveness check (no /proc)."""

    def _ps(self, stdout: str, rc: int = 0) -> Any:
        return subprocess.CompletedProcess(["ps"], rc, stdout, "")

    def test_running_state_is_alive(self) -> None:
        with patch.object(server_proc.subprocess, "run", return_value=self._ps("Ss\n")):
            assert server_proc._ps_pid_alive(4242) is True

    def test_zombie_state_is_not_alive(self) -> None:
        with patch.object(server_proc.subprocess, "run", return_value=self._ps("Z+\n")):
            assert server_proc._ps_pid_alive(4242) is False

    def test_gone_pid_is_not_alive(self) -> None:
        # The reaping race: ps no longer finds the pid (empty / non-zero exit).
        # This MUST read as not-alive — "ps can't find it" is not "still running".
        with patch.object(server_proc.subprocess, "run", return_value=self._ps("", rc=1)):
            assert server_proc._ps_pid_alive(4242) is False

    def test_ps_failure_assumes_alive(self) -> None:
        # Indeterminate (ps missing/timeout) → assume alive so stop() escalates
        # rather than declaring a live process dead.
        with patch.object(server_proc.subprocess, "run", side_effect=FileNotFoundError):
            assert server_proc._ps_pid_alive(4242) is True


class TestPidAlivePlatformDispatch:
    def test_no_proc_reaped_zombie_reads_not_alive(self) -> None:
        # End-to-end regression for the macOS reaping race: os.kill(0) still
        # succeeds (table entry lingers), /proc is absent, and ps now reports the
        # pid as gone. _pid_alive must return False, not True.
        with (
            patch.object(server_proc.os, "kill", return_value=None),
            patch.object(server_proc.Path, "is_dir", return_value=False),
            patch.object(
                server_proc.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(["ps"], 1, "", ""),
            ),
        ):
            assert server_proc._pid_alive(4242) is False

    def test_proc_present_uses_proc_not_ps(self) -> None:
        # Linux invariant: with /proc present, liveness comes from /proc and ps
        # is never consulted.
        with (
            patch.object(server_proc.os, "kill", return_value=None),
            patch.object(server_proc.Path, "is_dir", return_value=True),
            patch.object(server_proc.Path, "read_text", return_value="State:\tS (sleeping)\n"),
            patch.object(
                server_proc.subprocess, "run", side_effect=AssertionError("no ps on Linux")
            ),
        ):
            assert server_proc._pid_alive(4242) is True


# ---------------------------------------------------------------------------
# Real-process exercise of the macOS/BSD fallbacks.
#
# The classes above mock subprocess so the parsing runs everywhere. These pin
# the fallbacks against the actual OS — lsof/ps vs a real listener/process — so
# a parser that drifts from real tool output is caught. Skipped where the tool
# is unavailable, so they're safe on any runner.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("lsof") is None, reason="lsof not available")
class TestLsofAgainstRealListener:
    def test_lsof_finds_real_listener_pid(self) -> None:
        # Child binds an ephemeral port and reports it on stdout, so there is
        # no bind-race between picking the port and the child owning it.
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import socket,time\n"
                "s=socket.socket(); s.bind(('127.0.0.1',0)); s.listen()\n"
                "print(s.getsockname()[1], flush=True)\n"
                "time.sleep(30)\n",
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert child.stdout is not None
            port = int(child.stdout.readline().strip())
            # Exercises the real lsof fallback (the path macOS/BSD always take).
            assert server_proc._find_listening_pid_lsof(port) == child.pid
        finally:
            child.terminate()
            child.wait(timeout=5)


@pytest.mark.skipif(shutil.which("ps") is None, reason="ps not available")
class TestPsAgainstRealProcess:
    def test_ps_cmdline_of_self_contains_python(self) -> None:
        assert "python" in server_proc._ps_cmdline(os.getpid()).lower()

    def test_ps_reports_self_alive(self) -> None:
        # The live test process is obviously running (not gone, not a zombie).
        assert server_proc._ps_pid_alive(os.getpid()) is True


# ---------------------------------------------------------------------------
# port_holder_cmdline — classify a port holder (reclaim_stale_port covered below)
# ---------------------------------------------------------------------------


class TestPortHolderCmdline:
    def test_free_port_returns_none_empty(self) -> None:
        with patch.object(server_proc, "find_listening_pid", return_value=None):
            assert server_proc.port_holder_cmdline(47950) == (None, "")

    def test_returns_pid_and_cmdline(self) -> None:
        with (
            patch.object(server_proc, "find_listening_pid", return_value=4242),
            patch.object(server_proc, "_read_cmdline", return_value="uvicorn agentalloy.app"),
        ):
            assert server_proc.port_holder_cmdline(47950) == (4242, "uvicorn agentalloy.app")


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
        #
        # The child prints "ready" only AFTER installing the SIG_IGN handler,
        # and we block on that line before signaling. A fixed sleep here was
        # flaky: on a loaded/cold host, Python startup can exceed it, so SIGTERM
        # landed before the handler was installed and the default disposition
        # killed the child → stop() returned "term" instead of "kill".
        script = (
            "import signal, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "sys.stdout.write('ready\\n'); sys.stdout.flush()\n"
            "time.sleep(30)\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            assert proc.stdout is not None
            assert proc.stdout.readline().strip() == "ready"  # handler is installed
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
