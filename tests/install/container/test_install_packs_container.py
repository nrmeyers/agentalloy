# ruff: noqa: I001 -- testing private module members intentionally
"""Tests for install-packs container routing (UT-26..UT-30, UT-40, UT-41)."""

from __future__ import annotations

import argparse
import subprocess
from unittest.mock import patch

import pytest

from agentalloy.install.subcommands import install_packs


def _ns(**overrides) -> argparse.Namespace:  # type: ignore[no-untyped-def]
    defaults = dict(
        packs="python",
        no_restart=False,
        ignore_unknown=False,
        list=False,
        non_interactive=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _stat_missing() -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=1, stdout=b"", stderr=b"stat: cannot stat: No such file or directory\n"
    )


def _stat_fresh() -> subprocess.CompletedProcess:
    import time as _t

    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout=f"{int(_t.time())}\n".encode(), stderr=b""
    )


def _stat_stale() -> subprocess.CompletedProcess:
    import time as _t

    return subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=f"{int(_t.time()) - 4000}\n".encode(),  # ~66 min old; > 30 min threshold
        stderr=b"",
    )


def _stat_container_dead() -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[],
        returncode=125,
        stdout=b"",
        stderr=b"Error: no container with name agentalloy is running\n",
    )


# ---------------------------------------------------------------------------
# UT-26..UT-28: routing decision
# ---------------------------------------------------------------------------


class TestRouting:
    def test_ut26_routes_when_deployment_container(self) -> None:
        state = {
            "deployment": "container",
            "runtime_binary": "podman",
            "container_name": "agentalloy",
        }
        calls: list[list[str]] = []

        def fake_run(cmd, **_kw):  # type: ignore[no-untyped-def]
            calls.append(cmd)
            if "stat" in cmd:
                return _stat_missing()
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=b"", stderr=b"")

        with (
            patch("agentalloy.install.container_service.is_in_container", return_value=False),
            patch(
                "agentalloy.install.subcommands.install_packs.install_state.load_state",
                return_value=state,
            ),
            patch("subprocess.run", side_effect=fake_run),
        ):
            rc = install_packs._maybe_route_to_container(_ns(packs="python"))
        assert rc == 0
        # Exec command runs podman exec ... install-packs
        exec_calls = [c for c in calls if c[1] == "exec"]
        assert exec_calls, calls
        # First arg is the runtime binary
        assert exec_calls[-1][0] == "podman"
        # Container name is target
        assert "agentalloy" in exec_calls[-1]
        # install-packs invoked via uv run
        sh_payload = exec_calls[-1][-1]
        # shlex.quote leaves plain alphanumeric tokens unquoted.
        assert "install-packs --packs python" in sh_payload

    def test_ut27_returns_none_when_deployment_native(self) -> None:
        state = {"deployment": "native"}
        with (
            patch("agentalloy.install.container_service.is_in_container", return_value=False),
            patch(
                "agentalloy.install.subcommands.install_packs.install_state.load_state",
                return_value=state,
            ),
        ):
            assert install_packs._maybe_route_to_container(_ns()) is None

    def test_ut27b_returns_none_when_in_container(self) -> None:
        """No recursion: when we ARE inside the container, run locally."""
        state = {"deployment": "container"}
        with (
            patch("agentalloy.install.container_service.is_in_container", return_value=True),
            patch(
                "agentalloy.install.subcommands.install_packs.install_state.load_state",
                return_value=state,
            ),
        ):
            assert install_packs._maybe_route_to_container(_ns()) is None

    def test_ut28_returns_error_when_container_not_running(self) -> None:
        state = {
            "deployment": "container",
            "runtime_binary": "podman",
            "container_name": "agentalloy",
        }

        def fake_run(cmd, **_kw):  # type: ignore[no-untyped-def]
            # stat call → container dead; install never gets to run.
            if "stat" in cmd:
                return _stat_container_dead()
            # The exec install_packs call still fires after error stat? No —
            # _read_container_install_lock returns "error" and we continue to
            # the exec, which surfaces the runtime error itself.
            return subprocess.CompletedProcess(
                args=cmd, returncode=125, stdout=b"", stderr=b"no container"
            )

        with (
            patch("agentalloy.install.container_service.is_in_container", return_value=False),
            patch(
                "agentalloy.install.subcommands.install_packs.install_state.load_state",
                return_value=state,
            ),
            patch("subprocess.run", side_effect=fake_run),
        ):
            rc = install_packs._maybe_route_to_container(_ns(packs="python"))
        # Returns the runtime's non-zero exit (125) — host caller sees a hard fail.
        assert rc == 125


# ---------------------------------------------------------------------------
# UT-29..UT-30: concurrent install lock
# ---------------------------------------------------------------------------


class TestInstallLock:
    def test_ut29_fresh_lock_returns_busy(self) -> None:
        state = {
            "deployment": "container",
            "runtime_binary": "podman",
            "container_name": "agentalloy",
        }
        calls: list[list[str]] = []

        def fake_run(cmd, **_kw):  # type: ignore[no-untyped-def]
            calls.append(cmd)
            if "stat" in cmd:
                return _stat_fresh()
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=b"", stderr=b"")

        with (
            patch("agentalloy.install.container_service.is_in_container", return_value=False),
            patch(
                "agentalloy.install.subcommands.install_packs.install_state.load_state",
                return_value=state,
            ),
            patch("subprocess.run", side_effect=fake_run),
        ):
            rc = install_packs._maybe_route_to_container(_ns())
        assert rc == 2
        # No install attempt — only the stat call should have run.
        assert len(calls) == 1
        assert "stat" in calls[0]

    def test_ut30_stale_lock_is_cleared_and_install_proceeds(self) -> None:
        state = {
            "deployment": "container",
            "runtime_binary": "podman",
            "container_name": "agentalloy",
        }
        seq: list[str] = []

        def fake_run(cmd, **_kw):  # type: ignore[no-untyped-def]
            if "stat" in cmd:
                seq.append("stat")
                return _stat_stale()
            if "rm" in cmd and "/app/.install-packs-lock" in cmd:
                seq.append("rm")
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=b"", stderr=b"")
            seq.append("install")
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=b"", stderr=b"")

        with (
            patch("agentalloy.install.container_service.is_in_container", return_value=False),
            patch(
                "agentalloy.install.subcommands.install_packs.install_state.load_state",
                return_value=state,
            ),
            patch("subprocess.run", side_effect=fake_run),
        ):
            rc = install_packs._maybe_route_to_container(_ns())
        assert rc == 0
        assert seq == ["stat", "rm", "install"], seq


# ---------------------------------------------------------------------------
# UT-40, UT-41: container-side lock lifecycle in entrypoint script
# ---------------------------------------------------------------------------


class TestEntrypointInstallLock:
    """The fast-start entrypoint must wrap each pack install in a lock."""

    def test_ut40_lock_touched_before_install(self) -> None:
        from agentalloy.install.subcommands import container_runtime as cr

        script = cr._build_entrypoint_script("python,nodejs")  # pyright: ignore[reportPrivateUsage]
        touch_idx = script.find('touch "$INSTALL_LOCK"')
        run_idx = script.find("uv run agentalloy install-packs")
        assert touch_idx != -1 and run_idx != -1
        assert touch_idx < run_idx

    def test_ut41_lock_removed_after_install(self) -> None:
        from agentalloy.install.subcommands import container_runtime as cr

        script = cr._build_entrypoint_script("python")  # pyright: ignore[reportPrivateUsage]
        run_idx = script.find("uv run agentalloy install-packs")
        rm_idx = script.find('rm -f "$INSTALL_LOCK"')
        assert run_idx != -1 and rm_idx != -1
        assert run_idx < rm_idx


# ---------------------------------------------------------------------------
# IT-5: runtime binary from state is honored
# ---------------------------------------------------------------------------


class TestRuntimeBinaryFromState:
    @pytest.mark.parametrize("runtime", ["podman", "docker", "docker compose"])
    def test_it5_runtime_binary(self, runtime: str) -> None:
        state = {
            "deployment": "container",
            "runtime_binary": runtime,
            "container_name": "agentalloy",
        }
        seen: list[list[str]] = []

        def fake_run(cmd, **_kw):  # type: ignore[no-untyped-def]
            seen.append(cmd)
            if "stat" in cmd:
                return _stat_missing()
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=b"", stderr=b"")

        with (
            patch("agentalloy.install.container_service.is_in_container", return_value=False),
            patch(
                "agentalloy.install.subcommands.install_packs.install_state.load_state",
                return_value=state,
            ),
            patch("subprocess.run", side_effect=fake_run),
        ):
            install_packs._maybe_route_to_container(_ns())
        expected_binary = runtime.split()[0]
        assert all(c[0] == expected_binary for c in seen), seen
