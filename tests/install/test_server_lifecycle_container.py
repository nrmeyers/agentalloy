# pyright: reportPrivateUsage=false
"""Tests for the container leg of the ``server-*`` lifecycle verbs.

The host (native) path is exercised in ``test_server_proc.py``. Here we mock
``install_state.load_state`` to return a *container* deployment and assert each
verb drives the container runtime (``{runtime} start/stop/restart``) instead of
host processes — covering running / stopped / missing containers, a missing
runtime, and that the native path is untouched when deployment != container.
"""

from __future__ import annotations

import argparse
import subprocess
from typing import Any
from unittest.mock import patch

import pytest

from agentalloy.install import server_proc
from agentalloy.install.subcommands import (
    server_container,
    server_restart,
    server_start,
    server_status,
    server_stop,
)


def _proc(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["podman"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _container_state(**overrides: Any) -> dict[str, Any]:
    base = {
        "deployment": "container",
        "runtime_binary": "podman",
        "container_name": "agentalloy",
        "port": 47950,
    }
    base.update(overrides)
    return base


def _native_state(**overrides: Any) -> dict[str, Any]:
    base = {"deployment": "native", "port": 47950}
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# resolve_deployment
# ---------------------------------------------------------------------------


class TestResolveDeployment:
    def test_container_state_resolves_runtime_and_name(self) -> None:
        with patch.object(server_proc.install_state, "load_state", return_value=_container_state()):
            t = server_proc.resolve_deployment(None)
        assert t.deployment == "container"
        assert t.runtime == "podman"
        assert t.container_name == "agentalloy"
        assert t.port == 47950

    def test_none_deployment_is_native(self) -> None:
        with patch.object(server_proc.install_state, "load_state", return_value={"port": 47950}):
            t = server_proc.resolve_deployment(None)
        assert t.deployment == "native"

    def test_port_override_wins(self) -> None:
        with patch.object(server_proc.install_state, "load_state", return_value=_container_state()):
            t = server_proc.resolve_deployment(50000)
        assert t.port == 50000

    def test_missing_runtime_binary_falls_back_to_detect(self) -> None:
        st = _container_state(runtime_binary=None)
        with (
            patch.object(server_proc.install_state, "load_state", return_value=st),
            patch(
                "agentalloy.install.subcommands.container_runtime._detect_runtime_binary",
                return_value="docker",
            ),
        ):
            t = server_proc.resolve_deployment(None)
        assert t.runtime == "docker"

    def test_default_container_name_when_absent(self) -> None:
        st = _container_state(container_name=None)
        with patch.object(server_proc.install_state, "load_state", return_value=st):
            t = server_proc.resolve_deployment(None)
        assert t.container_name == "agentalloy"


# ---------------------------------------------------------------------------
# server-start
# ---------------------------------------------------------------------------


def _start_args() -> argparse.Namespace:
    return argparse.Namespace(port=None, host=server_proc.DEFAULT_HOST, wait=1.0)


class TestServerStartContainer:
    def test_missing_container_errors(self) -> None:
        with (
            patch.object(server_proc.install_state, "load_state", return_value=_container_state()),
            patch.object(server_container, "_state", return_value=""),
            patch(
                "agentalloy.install.subcommands.container_runtime._runtime_is_functional",
                return_value=True,
            ),
            patch.object(server_container, "_runtime_call") as call,
        ):
            rc = server_start._run(_start_args())
        assert rc == server_start.EXIT_USER
        call.assert_not_called()

    def test_running_container_is_noop(self) -> None:
        with (
            patch.object(server_proc.install_state, "load_state", return_value=_container_state()),
            patch.object(server_container, "_state", return_value="running"),
            patch(
                "agentalloy.install.subcommands.container_runtime._runtime_is_functional",
                return_value=True,
            ),
            patch.object(server_container, "_runtime_call") as call,
        ):
            rc = server_start._run(_start_args())
        assert rc == server_start.EXIT_OK
        call.assert_not_called()

    def test_stopped_container_started_and_ready(self) -> None:
        with (
            patch.object(server_proc.install_state, "load_state", return_value=_container_state()),
            patch.object(server_container, "_state", return_value="exited"),
            patch(
                "agentalloy.install.subcommands.container_runtime._runtime_is_functional",
                return_value=True,
            ),
            patch.object(server_container, "_runtime_call", return_value=_proc()) as call,
            patch.object(server_proc, "health_ready", return_value=True),
        ):
            rc = server_start._run(_start_args())
        assert rc == server_start.EXIT_OK
        assert call.call_args.args[1] == ["start", "agentalloy"]

    def test_start_succeeds_but_not_ready_is_system_error(self) -> None:
        with (
            patch.object(server_proc.install_state, "load_state", return_value=_container_state()),
            patch.object(server_container, "_state", return_value="exited"),
            patch(
                "agentalloy.install.subcommands.container_runtime._runtime_is_functional",
                return_value=True,
            ),
            patch.object(server_container, "_runtime_call", return_value=_proc()),
            patch.object(server_proc, "health_ready", return_value=False),
        ):
            rc = server_start._run(_start_args())
        assert rc == server_start.EXIT_SYSTEM

    def test_runtime_missing_errors(self) -> None:
        st = _container_state(runtime_binary=None)
        with (
            patch.object(server_proc.install_state, "load_state", return_value=st),
            patch(
                "agentalloy.install.subcommands.container_runtime._detect_runtime_binary",
                return_value=None,
            ),
            patch.object(server_container, "_runtime_call") as call,
        ):
            rc = server_start._run(_start_args())
        assert rc == server_start.EXIT_USER
        call.assert_not_called()

    def test_runtime_not_functional_errors(self) -> None:
        with (
            patch.object(server_proc.install_state, "load_state", return_value=_container_state()),
            patch(
                "agentalloy.install.subcommands.container_runtime._runtime_is_functional",
                return_value=False,
            ),
            patch.object(server_container, "_runtime_call") as call,
        ):
            rc = server_start._run(_start_args())
        assert rc == server_start.EXIT_USER
        call.assert_not_called()

    def test_native_path_untouched(self) -> None:
        with (
            patch.object(server_proc.install_state, "load_state", return_value=_native_state()),
            patch.object(server_proc, "start_background", return_value=4242) as sb,
            patch.object(server_proc, "wait_until_listening", return_value=True),
        ):
            rc = server_start._run(_start_args())
        assert rc == server_start.EXIT_OK
        sb.assert_called_once()


# ---------------------------------------------------------------------------
# server-stop
# ---------------------------------------------------------------------------


def _stop_args(json: bool = False) -> argparse.Namespace:
    return argparse.Namespace(port=None, timeout=10.0, json=json, quiet=False)


class TestServerStopContainer:
    def test_running_container_stopped(self) -> None:
        with (
            patch.object(server_proc.install_state, "load_state", return_value=_container_state()),
            patch.object(server_container, "_state", return_value="running"),
            patch(
                "agentalloy.install.subcommands.container_runtime._runtime_is_functional",
                return_value=True,
            ),
            patch.object(server_container, "_runtime_call", return_value=_proc()) as call,
        ):
            rc = server_stop._run(_stop_args())
        assert rc == server_stop.EXIT_OK
        assert call.call_args.args[1] == ["stop", "--time", "10", "agentalloy"]

    def test_already_stopped_is_noop(self) -> None:
        with (
            patch.object(server_proc.install_state, "load_state", return_value=_container_state()),
            patch.object(server_container, "_state", return_value="exited"),
            patch(
                "agentalloy.install.subcommands.container_runtime._runtime_is_functional",
                return_value=True,
            ),
            patch.object(server_container, "_runtime_call") as call,
        ):
            rc = server_stop._run(_stop_args())
        assert rc == server_stop.EXIT_OK
        call.assert_not_called()

    def test_missing_container_is_noop(self) -> None:
        with (
            patch.object(server_proc.install_state, "load_state", return_value=_container_state()),
            patch.object(server_container, "_state", return_value=""),
            patch(
                "agentalloy.install.subcommands.container_runtime._runtime_is_functional",
                return_value=True,
            ),
            patch.object(server_container, "_runtime_call") as call,
        ):
            rc = server_stop._run(_stop_args())
        assert rc == server_stop.EXIT_OK
        call.assert_not_called()

    def test_stop_failure_surfaces_stderr(self) -> None:
        with (
            patch.object(server_proc.install_state, "load_state", return_value=_container_state()),
            patch.object(server_container, "_state", return_value="running"),
            patch(
                "agentalloy.install.subcommands.container_runtime._runtime_is_functional",
                return_value=True,
            ),
            patch.object(
                server_container, "_runtime_call", return_value=_proc(returncode=1, stderr="boom")
            ),
        ):
            rc = server_stop._run(_stop_args())
        assert rc == server_stop.EXIT_USER

    def test_runtime_missing_errors(self) -> None:
        st = _container_state(runtime_binary=None)
        with (
            patch.object(server_proc.install_state, "load_state", return_value=st),
            patch(
                "agentalloy.install.subcommands.container_runtime._detect_runtime_binary",
                return_value=None,
            ),
        ):
            rc = server_stop._run(_stop_args())
        assert rc == server_stop.EXIT_USER

    def test_native_path_untouched(self) -> None:
        with (
            patch.object(server_proc.install_state, "load_state", return_value=_native_state()),
            patch.object(server_proc, "find_listening_pid", return_value=None),
        ):
            rc = server_stop._run(_stop_args())
        assert rc == server_stop.EXIT_OK


# ---------------------------------------------------------------------------
# server-restart
# ---------------------------------------------------------------------------


def _restart_args() -> argparse.Namespace:
    return argparse.Namespace(port=None, host=server_proc.DEFAULT_HOST, stop_timeout=10.0, wait=1.0)


class TestServerRestartContainer:
    def test_running_container_restarted(self) -> None:
        with (
            patch.object(server_proc.install_state, "load_state", return_value=_container_state()),
            patch.object(server_container, "_state", return_value="running"),
            patch(
                "agentalloy.install.subcommands.container_runtime._runtime_is_functional",
                return_value=True,
            ),
            patch.object(server_container, "_runtime_call", return_value=_proc()) as call,
            patch.object(server_proc, "health_ready", return_value=True),
        ):
            rc = server_restart._run(_restart_args())
        assert rc == server_restart.EXIT_OK
        assert call.call_args.args[1] == ["restart", "--time", "10", "agentalloy"]

    def test_stopped_container_is_started(self) -> None:
        with (
            patch.object(server_proc.install_state, "load_state", return_value=_container_state()),
            patch.object(server_container, "_state", return_value="exited"),
            patch(
                "agentalloy.install.subcommands.container_runtime._runtime_is_functional",
                return_value=True,
            ),
            patch.object(server_container, "_runtime_call", return_value=_proc()) as call,
            patch.object(server_proc, "health_ready", return_value=True),
        ):
            rc = server_restart._run(_restart_args())
        assert rc == server_restart.EXIT_OK
        assert call.call_args.args[1] == ["start", "agentalloy"]

    def test_missing_container_errors(self) -> None:
        with (
            patch.object(server_proc.install_state, "load_state", return_value=_container_state()),
            patch.object(server_container, "_state", return_value=""),
            patch(
                "agentalloy.install.subcommands.container_runtime._runtime_is_functional",
                return_value=True,
            ),
            patch.object(server_container, "_runtime_call") as call,
        ):
            rc = server_restart._run(_restart_args())
        assert rc == server_restart.EXIT_USER
        call.assert_not_called()

    def test_runtime_missing_errors(self) -> None:
        st = _container_state(runtime_binary=None)
        with (
            patch.object(server_proc.install_state, "load_state", return_value=st),
            patch(
                "agentalloy.install.subcommands.container_runtime._detect_runtime_binary",
                return_value=None,
            ),
        ):
            rc = server_restart._run(_restart_args())
        assert rc == server_restart.EXIT_USER

    def test_native_path_untouched(self) -> None:
        with (
            patch.object(server_proc.install_state, "load_state", return_value=_native_state()),
            patch.object(server_proc, "find_listening_pid", return_value=None),
            patch.object(server_proc, "start_background", return_value=4242) as sb,
            patch.object(server_proc, "wait_until_listening", return_value=True),
        ):
            rc = server_restart._run(_restart_args())
        assert rc == server_restart.EXIT_OK
        sb.assert_called_once()


# ---------------------------------------------------------------------------
# server-status
# ---------------------------------------------------------------------------


def _status_args(json: bool = False) -> argparse.Namespace:
    return argparse.Namespace(port=None, json=json, quiet=False)


class TestServerStatusContainer:
    def test_running_container_reports_reachable(self) -> None:
        with (
            patch.object(server_proc.install_state, "load_state", return_value=_container_state()),
            patch.object(server_container, "_state", return_value="running"),
            patch.object(server_proc, "health_ready", return_value=True),
        ):
            payload = server_container.status(server_proc.resolve_deployment(None))
        assert payload["deployment"] == "container"
        assert payload["state"] == "running"
        assert payload["reachable"] is True

    def test_stopped_container_not_reachable_skips_probe(self) -> None:
        with (
            patch.object(server_proc.install_state, "load_state", return_value=_container_state()),
            patch.object(server_container, "_state", return_value="exited"),
            patch.object(server_proc, "health_ready") as hr,
        ):
            payload = server_container.status(server_proc.resolve_deployment(None))
        assert payload["reachable"] is False
        hr.assert_not_called()

    def test_missing_runtime_reports_error(self) -> None:
        st = _container_state(runtime_binary=None)
        with (
            patch.object(server_proc.install_state, "load_state", return_value=st),
            patch(
                "agentalloy.install.subcommands.container_runtime._detect_runtime_binary",
                return_value=None,
            ),
        ):
            payload = server_container.status(server_proc.resolve_deployment(None))
        assert payload["reachable"] is False
        assert "error" in payload

    def test_run_dispatches_container(self) -> None:
        with (
            patch.object(server_proc.install_state, "load_state", return_value=_container_state()),
            patch.object(server_container, "_state", return_value="running"),
            patch.object(server_proc, "health_ready", return_value=True),
        ):
            rc = server_status._run(_status_args(json=True))
        assert rc == 0

    def test_native_path_untouched(self) -> None:
        info = server_proc.ServerInfo(port=47950, pid=4242, reachable=True)
        with (
            patch.object(server_proc.install_state, "load_state", return_value=_native_state()),
            patch.object(server_proc, "server_info", return_value=info) as si,
        ):
            rc = server_status._run(_status_args())
        assert rc == 0
        si.assert_called_once()


@pytest.mark.parametrize("state", sorted(server_container._STOPPED_STATES))
def test_stopped_states_treated_as_stopped(state: str) -> None:
    with (
        patch.object(server_proc.install_state, "load_state", return_value=_container_state()),
        patch.object(server_container, "_state", return_value=state),
        patch(
            "agentalloy.install.subcommands.container_runtime._runtime_is_functional",
            return_value=True,
        ),
        patch.object(server_container, "_runtime_call") as call,
    ):
        rc = server_stop._run(_stop_args())
    assert rc == server_stop.EXIT_OK
    call.assert_not_called()
