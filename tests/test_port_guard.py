"""The _free_default_port helper must spare processes that predate the session.

Regression test: the fixture used to SIGTERM whatever held port 47950 —
including a developer's real agentalloy service running outside pytest.
"""

from __future__ import annotations

import os
import time
from unittest.mock import patch

import pytest

from tests import conftest as root_conftest

_SS_LINE = 'LISTEN 0 2048 127.0.0.1:47950 0.0.0.0:* users:(("python",pid=4242,fd=13))\n'


def test_proc_start_epoch_resolves_own_pid() -> None:
    started = root_conftest._proc_start_epoch(os.getpid())
    assert started is not None
    assert started <= time.time()


def test_parse_etime_formats() -> None:
    # ps `etime` is [[DD-]HH:]MM:SS — portable across Linux and macOS/BSD,
    # which is why _proc_start_epoch uses it (macOS ps has no `etimes`).
    assert root_conftest._parse_etime("00:00") == 0
    assert root_conftest._parse_etime("01:30") == 90
    assert root_conftest._parse_etime("01:02:03") == 3723
    assert root_conftest._parse_etime("2-03:04:05") == 2 * 86400 + 3 * 3600 + 4 * 60 + 5
    assert root_conftest._parse_etime("") is None
    assert root_conftest._parse_etime("garbage") is None


def test_ps_start_epoch_uses_etime(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pin the macOS/BSD fallback: ps reports 60s elapsed → start ≈ now - 60.
    monkeypatch.setattr(root_conftest.subprocess, "check_output", lambda *a, **k: "01:00\n")
    started = root_conftest._ps_start_epoch(4242)
    assert started is not None
    assert abs((time.time() - 60) - started) < 5


def _run_kill_port_with(start_epoch: float | None) -> list[int]:
    killed: list[int] = []
    with (
        patch.object(root_conftest.subprocess, "check_output", return_value=_SS_LINE),
        patch.object(root_conftest, "_proc_start_epoch", return_value=start_epoch),
        patch.object(root_conftest.os, "kill", side_effect=lambda pid, sig: killed.append(pid)),
    ):
        root_conftest._kill_port(47950)
    return killed


def test_kill_port_spares_preexisting_process() -> None:
    assert _run_kill_port_with(root_conftest._SESSION_START_EPOCH - 3600) == []


def test_kill_port_spares_unknown_start_time() -> None:
    assert _run_kill_port_with(None) == []


def test_kill_port_kills_session_leaked_process() -> None:
    assert _run_kill_port_with(root_conftest._SESSION_START_EPOCH + 60) == [4242]


# ---------------------------------------------------------------------------
# server_proc.stop guard: the broader seam.
#
# _free_default_port only governs this conftest's own _kill_port. Production
# code paths a test can reach unmocked — uninstall(stop_services=True),
# server-stop, server-restart, wrap — all call server_proc.stop(pid) directly.
# uninstall even confirms the listener is agentalloy via /proc/<pid>/cmdline,
# so a developer's real `uvicorn agentalloy.app:app` matches and gets killed.
# The session-scoped _guard_server_proc_stop fixture wraps server_proc.stop to
# spare pre-session PIDs. These tests pin that behavior so it cannot regress.
# ---------------------------------------------------------------------------


def _run_guarded_stop_with(start_epoch: float | None) -> list[int]:
    """Call the active (guarded) server_proc.stop and record which PIDs
    actually reached os.kill."""
    from agentalloy.install import server_proc

    killed: list[int] = []
    with (
        patch.object(root_conftest, "_proc_start_epoch", return_value=start_epoch),
        patch.object(server_proc.os, "kill", side_effect=lambda pid, sig: killed.append(pid)),
        # After SIGTERM, stop() polls _pid_alive; pretend the process exited
        # so the real stop returns promptly without escalating to SIGKILL.
        patch.object(server_proc, "_pid_alive", return_value=False),
    ):
        # server_proc.stop here is the guarded wrapper installed by the
        # session-scoped autouse fixture in conftest.
        server_proc.stop(4242)
    return killed


def test_guarded_stop_spares_preexisting_process() -> None:
    assert _run_guarded_stop_with(root_conftest._SESSION_START_EPOCH - 3600) == []


def test_guarded_stop_kills_session_leaked_process() -> None:
    assert _run_guarded_stop_with(root_conftest._SESSION_START_EPOCH + 60) == [4242]


def test_uninstall_does_not_kill_preexisting_server() -> None:
    """End-to-end: uninstall(stop_services=True) must not SIGTERM a server
    that predates the session — the exact path test_adversarial.py reached
    unmocked, which motivated the guard.
    """
    from agentalloy.install import server_proc
    from agentalloy.install.subcommands import uninstall as uninstall_mod

    killed: list[int] = []
    with (
        patch.object(server_proc, "find_listening_pid", return_value=4242),
        patch.object(
            root_conftest,
            "_proc_start_epoch",
            return_value=root_conftest._SESSION_START_EPOCH - 3600,
        ),
        patch.object(server_proc.os, "kill", side_effect=lambda pid, sig: killed.append(pid)),
        patch.object(server_proc, "_pid_alive", return_value=False),
    ):
        result = uninstall_mod.uninstall(force=True, stop_services=True)

    assert killed == [], "uninstall killed a pre-session server"
    assert isinstance(result, dict)
