"""The _free_default_port helper must spare processes that predate the session.

Regression test: the fixture used to SIGTERM whatever held port 47950 —
including a developer's real agentalloy service running outside pytest.
"""

from __future__ import annotations

import os
import time
from unittest.mock import patch

from tests import conftest as root_conftest

_SS_LINE = 'LISTEN 0 2048 127.0.0.1:47950 0.0.0.0:* users:(("python",pid=4242,fd=13))\n'


def test_proc_start_epoch_resolves_own_pid() -> None:
    started = root_conftest._proc_start_epoch(os.getpid())
    assert started is not None
    assert started <= time.time()


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
