# ruff: noqa: I001, PLC0415 -- testing private module members intentionally
"""Tests for the preflight container phase."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from agentalloy.install.subcommands import preflight
from agentalloy.install.subcommands.preflight import (
    _check_cli_on_path,  # pyright: ignore[reportPrivateUsage]
    _check_llama_server_present,  # pyright: ignore[reportPrivateUsage]
    _check_llama_server_reachable,  # pyright: ignore[reportPrivateUsage]
    _find_agentalloy_binary,  # pyright: ignore[reportPrivateUsage]
    _try_brew_install,  # pyright: ignore[reportPrivateUsage]
)


class TestBrewAutoInstall:
    """Test macOS brew auto-install behavior in runner-phase checks.

    Brew auto-install is gated behind AGENTALLOY_PREFLIGHT_AUTO_INSTALL=1
    (opt-in). The autouse fixture below enables that opt-in for every test in
    this class; an explicit test verifies the gate is honored when the env
    var is unset.
    """

    @pytest.fixture(autouse=True)
    def _enable_auto_install_optin(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("AGENTALLOY_PREFLIGHT_AUTO_INSTALL", "1")

    def test_try_brew_install_non_macos_noop(self):
        with patch("sys.platform", "linux"):
            ok, err = _try_brew_install("some-cask", cask=True)
        assert ok is False
        assert err == "not macOS"

    def test_try_brew_install_no_brew_binary(self):
        with (
            patch("sys.platform", "darwin"),
            patch("agentalloy.install.subcommands.preflight.shutil.which", return_value=None),
        ):
            ok, err = _try_brew_install("llama.cpp")
        assert ok is False
        assert err == "brew not on PATH"

    def test_try_brew_install_disabled_without_optin(self, monkeypatch: pytest.MonkeyPatch):
        """Without AGENTALLOY_PREFLIGHT_AUTO_INSTALL=1, brew install is a no-op."""
        monkeypatch.delenv("AGENTALLOY_PREFLIGHT_AUTO_INSTALL", raising=False)
        with (
            patch("sys.platform", "darwin"),
            patch(
                "agentalloy.install.subcommands.preflight.shutil.which",
                return_value="/opt/homebrew/bin/brew",
            ),
        ):
            ok, err = _try_brew_install("some-cask", cask=True)
        assert ok is False
        assert "auto-install disabled" in err

    def test_try_brew_install_redirects_stdout_to_stderr(self):
        """brew stdout must not corrupt --json output."""
        import sys as _sys

        captured: dict[str, Any] = {}

        def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
            captured["stdout"] = kwargs.get("stdout")
            captured["cmd"] = cmd
            return MagicMock(returncode=0)

        with (
            patch("sys.platform", "darwin"),
            patch(
                "agentalloy.install.subcommands.preflight.shutil.which",
                return_value="/opt/homebrew/bin/brew",
            ),
            patch("agentalloy.install.subcommands.preflight.subprocess.run", side_effect=fake_run),
        ):
            ok, err = _try_brew_install("some-cask", cask=True)
        assert ok is True
        assert err is None
        assert captured["stdout"] is _sys.stderr
        assert captured["cmd"] == ["brew", "install", "--cask", "some-cask"]

    def test_llama_server_brew_installs_then_resolves(self):
        which_results = {
            "llama-server": iter([None, "/opt/homebrew/bin/llama-server"]),
            "brew": iter(["/opt/homebrew/bin/brew", "/opt/homebrew/bin/brew"]),
        }

        with (
            patch("sys.platform", "darwin"),
            patch(
                "agentalloy.install.subcommands.preflight.shutil.which",
                side_effect=lambda cmd: next(which_results[cmd]),
            ),
            patch(
                "agentalloy.install.subcommands.preflight.subprocess.run",
                return_value=MagicMock(returncode=0),
            ),
        ):
            result = _check_llama_server_present()
        assert result["passed"] is True
        assert "installed via brew" in result["detail"]

    def test_llama_server_brew_succeeds_but_binary_still_missing(self):
        which_results = {
            "llama-server": iter([None, None]),
            "brew": iter(["/opt/homebrew/bin/brew", "/opt/homebrew/bin/brew"]),
        }

        with (
            patch("sys.platform", "darwin"),
            patch(
                "agentalloy.install.subcommands.preflight.shutil.which",
                side_effect=lambda cmd: next(which_results[cmd]),
            ),
            patch(
                "agentalloy.install.subcommands.preflight.subprocess.run",
                return_value=MagicMock(returncode=0),
            ),
        ):
            result = _check_llama_server_present()
        assert result["passed"] is False
        assert "succeeded but `llama-server` is still not on PATH" in result["error"]


class TestLlamaServerReachable:
    """``_check_llama_server_reachable`` probes the embed server's /health.

    llama-server has no ``/api/tags`` (that's Ollama) — the readiness check
    must hit ``/health`` on the configured embed base URL (default 47951).
    """

    def test_targets_health_on_default_embed_port(self):
        """No RUNTIME_EMBED_BASE_URL in env → defaults to localhost:47951/health."""
        captured: dict[str, Any] = {}

        class _Resp:
            def __enter__(self):  # type: ignore[no-untyped-def]
                return self

            def __exit__(self, *a):  # type: ignore[no-untyped-def]
                return False

            def read(self, _n=None):  # type: ignore[no-untyped-def]
                return b"o"

        def fake_urlopen(req, **kwargs):  # type: ignore[no-untyped-def]
            captured["url"] = req.full_url
            return _Resp()

        with (
            patch(
                "agentalloy.install.subcommands.preflight.install_state.parse_env_file",
                return_value={},
            ),
            patch("agentalloy.install.subcommands.preflight.urlopen", side_effect=fake_urlopen),
        ):
            result = _check_llama_server_reachable()
        assert result["passed"] is True
        assert captured["url"] == "http://localhost:47951/health"

    def test_honors_runtime_embed_base_url(self):
        captured: dict[str, Any] = {}

        def fake_urlopen(req, **kwargs):  # type: ignore[no-untyped-def]
            captured["url"] = req.full_url
            raise OSError("connection refused")

        with (
            patch(
                "agentalloy.install.subcommands.preflight.install_state.parse_env_file",
                return_value={"RUNTIME_EMBED_BASE_URL": "http://localhost:9999"},
            ),
            patch("agentalloy.install.subcommands.preflight.urlopen", side_effect=fake_urlopen),
        ):
            result = _check_llama_server_reachable()
        assert result["passed"] is False
        assert captured["url"] == "http://localhost:9999/health"
        # Remediation references llama-server, not Ollama.
        assert "llama-server" in result["remediation"]
        assert "api/tags" not in result["remediation"]


# ---------------------------------------------------------------------------
# New container-phase checks (preflight refactor)
# ---------------------------------------------------------------------------


class TestCheckRuntimeBinary:
    """UT-11, UT-12, UT-13: _check_runtime_binary() — podman preferred, docker fallback."""

    def test_podman_on_path_passes(self):
        """UT-11: _check_runtime_binary() passes when podman is on PATH and functional."""
        from agentalloy.install.subcommands.preflight import _check_runtime_binary

        with (
            patch(
                "agentalloy.install.subcommands.preflight.shutil.which",
                return_value="/usr/bin/podman",
            ),
            patch(
                "agentalloy.install.subcommands.container_runtime._runtime_is_functional",
                return_value=True,
            ),
        ):
            result = _check_runtime_binary("podman")
        assert result["passed"] is True
        assert "podman" in result["detail"]

    def test_only_docker_on_path_passes(self):
        """UT-12: _check_runtime_binary() passes when docker is on PATH and functional."""
        from agentalloy.install.subcommands.preflight import _check_runtime_binary

        with (
            patch(
                "agentalloy.install.subcommands.preflight.shutil.which",
                return_value="/usr/bin/docker",
            ),
            patch(
                "agentalloy.install.subcommands.container_runtime._runtime_is_functional",
                return_value=True,
            ),
        ):
            result = _check_runtime_binary("docker")
        assert result["passed"] is True
        assert "docker" in result["detail"]

    def test_present_but_not_functional_fails(self):
        """_check_runtime_binary() fails when the runtime is on PATH but not responding."""
        from agentalloy.install.subcommands.preflight import _check_runtime_binary

        with (
            patch(
                "agentalloy.install.subcommands.preflight.shutil.which",
                return_value="/usr/bin/podman",
            ),
            patch(
                "agentalloy.install.subcommands.container_runtime._runtime_is_functional",
                return_value=False,
            ),
        ):
            result = _check_runtime_binary("podman")
        assert result["passed"] is False
        assert result["severity"] == "fatal"
        assert "not responding" in result["error"]

    def test_neither_binary_fails(self):
        """UT-13: _check_runtime_binary() fails when neither podman nor docker on PATH."""
        from agentalloy.install.subcommands.preflight import _check_runtime_binary

        result = _check_runtime_binary(None)
        assert result["passed"] is False
        assert result["severity"] == "fatal"
        assert "remediation" in result
        assert "podman" in result["error"] or "docker" in result["error"]

    def test_runtime_not_on_path_fails(self):
        """_check_runtime_binary() fails when the binary is not on PATH."""
        from agentalloy.install.subcommands.preflight import _check_runtime_binary

        with patch("agentalloy.install.subcommands.preflight.shutil.which", return_value=None):
            result = _check_runtime_binary("podman")
        assert result["passed"] is False
        assert "not found on PATH" in result["error"]


class TestCheckNameConflicts:
    """UT-17, UT-18: _check_name_conflicts() — existing container detection."""

    def test_detects_existing_container(self):
        """UT-17: _check_name_conflicts() detects existing agentalloy container."""
        from agentalloy.install.subcommands.preflight import _check_name_conflicts

        def run_side_effect(cmd, **kwargs):
            mock = MagicMock()
            mock.returncode = 0
            # bughunt 4.10: output is now "ID\tName"; only an exact-name
            # collision (parts[1] == "agentalloy") fails the check.
            mock.stdout = "abc123def456\tagentalloy"
            return mock

        with patch(
            "agentalloy.install.subcommands.preflight.subprocess.run", side_effect=run_side_effect
        ):
            result = _check_name_conflicts("podman")
        assert result["passed"] is False
        assert "agentalloy" in result["error"].lower() or "already" in result["error"].lower()

    def test_no_conflict_passes(self):
        """UT-18: _check_name_conflicts() passes when no conflict."""
        from agentalloy.install.subcommands.preflight import _check_name_conflicts

        def run_side_effect(cmd, **kwargs):
            mock = MagicMock()
            mock.returncode = 0
            mock.stdout = ""
            return mock

        with patch(
            "agentalloy.install.subcommands.preflight.subprocess.run", side_effect=run_side_effect
        ):
            result = _check_name_conflicts("podman")
        assert result["passed"] is True


class TestCheckVolumeExists:
    """_check_volume_exists() — existing volume detection."""

    def test_detects_existing_volume(self):
        """Volume already exists — should pass (volume creation is idempotent)."""
        from agentalloy.install.subcommands.preflight import _check_volume_exists

        def run_side_effect(cmd, **kwargs):
            mock = MagicMock()
            mock.returncode = 0
            mock.stdout = "agentalloy-data"
            return mock

        with patch(
            "agentalloy.install.subcommands.preflight.subprocess.run", side_effect=run_side_effect
        ):
            result = _check_volume_exists("podman")
        assert result["passed"] is True

    def test_no_volume_passes(self):
        """Volume does not exist — OK for preflight (creation happens later)."""
        from agentalloy.install.subcommands.preflight import _check_volume_exists

        def run_side_effect(cmd, **kwargs):
            mock = MagicMock()
            mock.returncode = 1
            mock.stderr = "Error: no such volume: agentalloy-data"
            return mock

        with patch(
            "agentalloy.install.subcommands.preflight.subprocess.run", side_effect=run_side_effect
        ):
            result = _check_volume_exists("podman")
        assert result["passed"] is True


class TestRunPreflightContainerPhase:
    """run_preflight(phase='container') resolves the runtime functionally."""

    @staticmethod
    def _patch_cheap_checks(stack_runtime: list[str | None]):
        """Patch network/disk/port checks to pass and record the runtime passed to
        the conflict/volume checks."""
        from agentalloy.install.subcommands import preflight as pf

        def _ok(name):
            return {"name": name, "passed": True, "severity": "info", "detail": ""}

        return (
            patch.object(pf, "_check_ghcr_reachable", lambda: _ok("ghcr")),
            patch.object(pf, "_check_disk_space", lambda: _ok("disk")),
            patch.object(pf, "_check_port_free", lambda port: _ok("port")),
            patch.object(
                pf,
                "_check_name_conflicts",
                lambda rt: (stack_runtime.append(rt), _ok("name_conflicts"))[1],
            ),
            patch.object(pf, "_check_volume_exists", lambda rt: _ok("volume")),
        )

    def test_detects_functional_docker_when_runtime_none(self):
        """runtime=None → uses the functional-aware detector (docker), not presence order."""
        from contextlib import ExitStack

        from agentalloy.install.subcommands.preflight import run_preflight

        seen: list[str | None] = []
        with ExitStack() as stack:
            stack.enter_context(
                patch(
                    "agentalloy.install.subcommands.container_runtime._detect_runtime_binary",
                    return_value="docker",
                )
            )
            stack.enter_context(
                patch(
                    "agentalloy.install.subcommands.preflight.shutil.which",
                    side_effect=lambda n: f"/usr/bin/{n}",
                )
            )
            stack.enter_context(
                patch(
                    "agentalloy.install.subcommands.container_runtime._runtime_is_functional",
                    return_value=True,
                )
            )
            for cm in self._patch_cheap_checks(seen):
                stack.enter_context(cm)
            result = run_preflight(phase="container", runtime=None)

        runtime_check = next(c for c in result["checks"] if c["name"] == "runtime_binary")
        assert runtime_check["passed"] is True
        assert "docker" in runtime_check["detail"]
        assert seen == ["docker"]  # conflict check ran against docker, not a podman default

    def test_skips_conflict_checks_when_no_runtime(self):
        """No runtime detectable → fatal runtime_binary check, conflict/volume checks skipped."""
        from contextlib import ExitStack

        from agentalloy.install.subcommands.preflight import run_preflight

        seen: list[str | None] = []
        with ExitStack() as stack:
            stack.enter_context(
                patch(
                    "agentalloy.install.subcommands.container_runtime._detect_runtime_binary",
                    return_value=None,
                )
            )
            for cm in self._patch_cheap_checks(seen):
                stack.enter_context(cm)
            result = run_preflight(phase="container", runtime=None)

        names = {c["name"] for c in result["checks"]}
        runtime_check = next(c for c in result["checks"] if c["name"] == "runtime_binary")
        assert runtime_check["passed"] is False
        assert runtime_check["severity"] == "fatal"
        assert "name_conflicts" not in names
        assert "volume" not in names
        assert seen == []  # no podman default fabricated


class TestCheckCliOnPath:
    """``_check_cli_on_path`` / ``_find_agentalloy_binary`` — anomaly A5.

    A uv tool install whose entry point was not linked into ``~/.local/bin``
    leaves the real binary in the tool venv's own ``bin/``. The PATH
    remediation must point at the dir that ACTUALLY holds the binary, not the
    canonical ``~/.local/bin`` (which would be a wrong, copy-pasteable line).
    """

    def test_passes_when_resolvable_and_canonical_dir_on_path(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        home = Path.home()
        monkeypatch.setenv("PATH", f"{home}/.local/bin:/usr/bin")
        monkeypatch.setattr(
            preflight.shutil,
            "which",
            lambda name: f"{home}/.local/bin/agentalloy" if name == "agentalloy" else None,
        )
        result = _check_cli_on_path()
        assert result["passed"] is True
        assert "agentalloy at" in result["detail"]

    def test_remediation_points_at_uv_tool_venv_bin_when_not_on_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """A5: binary only in the uv tool venv's bin/ → remediation exports THAT dir."""
        tool_dir = tmp_path / "uvtools"
        bin_dir = tool_dir / "agentalloy" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "agentalloy").write_text("#!/bin/sh\nexit 0\n")

        # Not resolvable on PATH, and the canonical dir is not in PATH.
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setenv("UV_TOOL_DIR", str(tool_dir))
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        monkeypatch.setattr(preflight.shutil, "which", lambda name: None)

        result = _check_cli_on_path()
        assert result["passed"] is False
        # The remediation must point at the real bin dir (resolved), not the
        # canonical ~/.local/bin which does not hold the binary.
        assert f'export PATH="{bin_dir.resolve()}:$PATH"' in result["remediation"]
        canonical = str(Path.home() / ".local" / "bin")
        assert f'export PATH="{canonical}:$PATH"' not in result["remediation"]

    def test_remediation_falls_back_to_canonical_dir_when_binary_not_found(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """Binary nowhere to be found → remediation falls back to ~/.local/bin."""
        # Empty XDG_DATA_HOME so the default uv tools location is empty; no UV_TOOL_DIR.
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "empty-xdg"))
        monkeypatch.delenv("UV_TOOL_DIR", raising=False)
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setattr(preflight.shutil, "which", lambda name: None)

        result = _check_cli_on_path()
        assert result["passed"] is False
        canonical = str(Path.home() / ".local" / "bin")
        assert f'export PATH="{canonical}:$PATH"' in result["remediation"]

    def test_find_binary_returns_path_when_on_path(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(preflight.shutil, "which", lambda name: "/usr/local/bin/agentalloy")
        assert _find_agentalloy_binary() == Path("/usr/local/bin/agentalloy")

    def test_find_binary_none_when_nowhere(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "empty"))
        monkeypatch.delenv("UV_TOOL_DIR", raising=False)
        monkeypatch.setattr(preflight.shutil, "which", lambda name: None)
        assert _find_agentalloy_binary() is None
