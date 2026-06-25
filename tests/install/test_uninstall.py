# ruff: noqa: I001, PLC0415 -- testing private module members intentionally
"""Tests for the uninstall subcommand (container branch)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from agentalloy.install.subcommands.uninstall import (
    _remove_sentinel_block,  # type: ignore[attr-defined]
    _extract_sentinel_content,  # type: ignore[attr-defined]
    _stop_container_stack,  # type: ignore[attr-defined]
    _remove_compose_volumes,  # type: ignore[attr-defined]
    _remove_container_image,  # type: ignore[attr-defined]
    _remove_agentalloy_cache,  # type: ignore[attr-defined]
    _COMPOSE_NAMED_VOLUMES,  # type: ignore[attr-defined]
)


@pytest.fixture(autouse=True)
def _fake_home_for_wiring(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """uninstall() resolves ``~/.agentalloy/claude-code-env.sh`` and the
    claude-code settings via ``Path.home()`` and DELETES them — every test in
    this module must see a throwaway home, or the suite destroys the developer's
    real wiring (tripwire: ``_guard_real_home_wiring`` in tests/conftest.py).
    Mirrors the fixture in test_adversarial.py / test_wire_harness.py."""
    home = tmp_path / "fake-home"
    home.mkdir(exist_ok=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


# Typed helper for mock_which.side_effect to avoid pyright reportUnknownLambdaType
WhichSideEffect = Callable[[str], str | None]


def _which_map(**mapping: str) -> WhichSideEffect:
    """Create a typed which side_effect from a mapping."""

    def _which(name: str) -> str | None:
        return mapping.get(name)

    return _which


def _which_single(target: str, path: str) -> WhichSideEffect:
    """Create a typed which side_effect that returns path only for target."""

    def _which(name: str) -> str | None:
        return path if name == target else None

    return _which


def _which_none() -> WhichSideEffect:
    """Create a typed which side_effect that always returns None."""

    def _which(name: str) -> str | None:
        return None

    return _which


class TestContainerUninstall:
    """Test container-specific uninstall logic."""

    def test_container_stop_and_rm_on_container_deployment(self, tmp_path: Path):
        """State with deployment='container' stops and removes the container."""
        state: dict[str, Any] = {
            "deployment": "container",
            "runtime_binary": "podman",
        }
        warnings: list[str] = []

        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            actions = _stop_container_stack(state, warnings)

        # Should call subprocess twice: stop, then rm
        assert mock_run.call_count == 2
        stop_args = mock_run.call_args_list[0][0][0]
        assert stop_args[0] == "podman"
        assert stop_args[1] == "stop"
        assert stop_args[2] == "agentalloy"
        rm_args = mock_run.call_args_list[1][0][0]
        assert rm_args[0] == "podman"
        assert rm_args[1] == "rm"
        assert rm_args[2] == "-f"
        assert rm_args[3] == "agentalloy"

        assert len(actions) == 2
        assert actions[0]["action"] == "container_stopped"
        assert actions[1]["action"] == "container_removed"
        assert not warnings

    def test_container_stop_and_rm_docker(self, tmp_path: Path):
        """Docker container variant works identically."""
        state: dict[str, Any] = {
            "deployment": "container",
            "runtime_binary": "docker",
        }
        warnings: list[str] = []

        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            actions = _stop_container_stack(state, warnings)

        stop_args = mock_run.call_args_list[0][0][0]
        assert stop_args[0] == "docker"
        rm_args = mock_run.call_args_list[1][0][0]
        assert rm_args[0] == "docker"
        assert actions[0]["action"] == "container_stopped"
        assert actions[1]["action"] == "container_removed"

    def test_container_stop_and_rm_custom_name(self, tmp_path: Path):
        """Custom container_name from state is used."""
        state: dict[str, Any] = {
            "deployment": "container",
            "runtime_binary": "podman",
            "container_name": "my-agentalloy",
        }
        warnings: list[str] = []

        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            actions = _stop_container_stack(state, warnings)

        stop_args = mock_run.call_args_list[0][0][0]
        assert stop_args[2] == "my-agentalloy"
        assert len(actions) == 2

    def test_compose_down_skipped_native(self):
        """Native deployment with no leftover container does nothing.

        The corpse sweep still runs (an interrupted container attempt can
        precede a native install), but with no matching container it must
        produce no actions and no subprocess stop/rm calls.
        """
        state: dict[str, Any] = {
            "deployment": "native",
        }
        warnings: list[str] = []

        with (
            patch(
                "agentalloy.install.subcommands.container_runtime._detect_runtime_binary",
                return_value="podman",
            ),
            patch(
                "agentalloy.install.subcommands.container_runtime._list_conflicting_containers",
                return_value=[],
            ),
            patch("subprocess.run") as mock_run,
        ):
            actions = _stop_container_stack(state, warnings)

        mock_run.assert_not_called()
        assert actions == []

    def test_compose_down_skipped_no_deployment_no_runtime(self):
        """No deployment recorded and no container runtime → clean no-op."""
        state: dict[str, Any] = {}
        warnings: list[str] = []
        with patch(
            "agentalloy.install.subcommands.container_runtime._detect_runtime_binary",
            return_value=None,
        ):
            actions = _stop_container_stack(state, warnings)
        assert actions == []
        assert warnings == []

    def test_unrecorded_container_corpse_is_removed(self):
        """An interrupted container install leaves deployment unset in state
        while its Exited container survives, holding the port reservation.
        Teardown must find it by name and remove it anyway."""
        state: dict[str, Any] = {"port": 47950}
        warnings: list[str] = []
        with (
            patch(
                "agentalloy.install.subcommands.container_runtime._detect_runtime_binary",
                return_value="podman",
            ),
            patch(
                "agentalloy.install.subcommands.container_runtime._list_conflicting_containers",
                return_value=[("agentalloy", "Exited (1) 2 days ago")],
            ),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stderr=b"")
            actions = _stop_container_stack(state, warnings)

        commands = [c.args[0] for c in mock_run.call_args_list]
        assert ["podman", "stop", "agentalloy"] in commands
        assert ["podman", "rm", "-f", "agentalloy"] in commands
        assert any("interrupted install" in w for w in warnings)
        assert any(a.get("action") == "container_stopped" for a in actions)

    def test_unrecorded_foreign_port_holder_left_with_warning(self):
        """A container publishing our port but not named ours is NOT removed —
        warn with a manual remediation instead."""
        state: dict[str, Any] = {"port": 47950}
        warnings: list[str] = []
        with (
            patch(
                "agentalloy.install.subcommands.container_runtime._detect_runtime_binary",
                return_value="podman",
            ),
            patch(
                "agentalloy.install.subcommands.container_runtime._list_conflicting_containers",
                return_value=[("someone-elses-app", "Up 2 hours")],
            ),
            patch("subprocess.run") as mock_run,
        ):
            actions = _stop_container_stack(state, warnings)

        mock_run.assert_not_called()
        assert actions == []
        assert any("someone-elses-app" in w and "podman rm" in w for w in warnings)

    def test_container_stop_missing_runtime_warns(self, tmp_path: Path):
        """OSError on stop and rm adds warnings for both."""
        state: dict[str, Any] = {
            "deployment": "container",
            "runtime_binary": "podman",
        }
        warnings: list[str] = []

        with patch("subprocess.run", side_effect=OSError("No such file: podman")):
            actions = _stop_container_stack(state, warnings)

        # Both stop and rm fail
        assert len(warnings) == 2
        assert "binary not found" in warnings[0].lower()
        assert "binary not found" in warnings[1].lower()
        assert actions[0]["action"] == "container_stop_skipped"
        assert actions[1]["action"] == "container_rm_skipped"

    def test_container_stop_none_in_state_warns(self):
        """runtime_binary is None (old/corrupt state)."""
        state: dict[str, Any] = {
            "deployment": "container",
            "runtime_binary": None,
        }
        warnings: list[str] = []
        actions = _stop_container_stack(state, warnings)

        assert len(warnings) == 1
        assert "runtime_binary" in warnings[0].lower()
        assert actions == []

    def test_container_stop_missing_runtime_label_warns(self):
        """runtime_binary is missing in state."""
        state: dict[str, Any] = {
            "deployment": "container",
        }
        warnings: list[str] = []
        actions = _stop_container_stack(state, warnings)

        assert len(warnings) == 1
        assert "runtime_binary" in warnings[0].lower()
        assert actions == []

    def test_remove_compose_volumes_runs_volume_rm_per_named_volume(self):
        """`volume rm -f` runs for each named volume declared in compose.yaml.
        Without this, fresh reinstalls silently reuse the prior corpus.
        """
        state: dict[str, Any] = {
            "deployment": "container",
            "runtime_binary": "podman",
        }
        warnings: list[str] = []
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            actions = _remove_compose_volumes(state, warnings)

        assert mock_run.call_count == len(_COMPOSE_NAMED_VOLUMES)
        for call, expected_vol in zip(mock_run.call_args_list, _COMPOSE_NAMED_VOLUMES, strict=True):
            argv = call.args[0]
            assert argv[0] == "podman"
            assert argv[1] == "volume"
            assert argv[2] == "rm"
            assert argv[3] == "-f"
            assert argv[4] == expected_vol
        assert all(a["action"] == "volume_removed" for a in actions)
        assert not warnings

    def test_remove_compose_volumes_skips_native_deployment(self):
        """Volume cleanup is container-only — native installs never created
        the named volumes."""
        state: dict[str, Any] = {"deployment": "native"}
        warnings: list[str] = []
        with patch("subprocess.run") as mock_run:
            actions = _remove_compose_volumes(state, warnings)
        mock_run.assert_not_called()
        assert actions == []
        assert not warnings

    def test_remove_compose_volumes_handles_missing_volume(self):
        """`volume rm` of a non-existent volume should be silent (idempotent),
        not surface a warning. Both podman ("no such volume") and docker
        ("not found") error strings are recognized."""
        state: dict[str, Any] = {
            "deployment": "container",
            "runtime_binary": "docker",
        }
        warnings: list[str] = []

        def fake_run(argv: Any, **kwargs: Any) -> MagicMock:  # type: ignore[no-untyped-def]
            m = MagicMock()
            m.returncode = 1
            m.stderr = "Error: no such volume: agentalloy-data\n"
            return m

        with patch("subprocess.run", side_effect=fake_run):
            actions = _remove_compose_volumes(state, warnings)

        assert all(a["action"] == "volume_already_gone" for a in actions)
        assert not warnings

    def test_remove_compose_volumes_warns_on_unresolved_binary(self):
        """When state doesn't have enough info to resolve the runtime
        binary, emit a manual-cleanup hint instead of silently no-op'ing."""
        state: dict[str, Any] = {"deployment": "container"}  # no runtime_binary
        warnings: list[str] = []
        with patch("subprocess.run") as mock_run:
            actions = _remove_compose_volumes(state, warnings)
        mock_run.assert_not_called()
        assert actions == []
        assert len(warnings) == 1
        assert "agentalloy-data" in warnings[0]

    def test_container_stop_invalid_label_warns(self, tmp_path: Path):
        """Empty runtime_binary label is rejected."""
        state: dict[str, Any] = {
            "deployment": "container",
            "runtime_binary": "  ",  # whitespace-only is effectively empty
        }
        warnings: list[str] = []
        _actions = _stop_container_stack(state, warnings)

        assert len(warnings) == 1
        assert "Invalid" in warnings[0] or "invalid" in warnings[0].lower()

    def test_container_stop_failure_warns(self, tmp_path: Path):
        """subprocess returns non-zero on stop and rm, warnings added."""
        state: dict[str, Any] = {
            "deployment": "container",
            "runtime_binary": "podman",
        }
        warnings: list[str] = []

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "Error: container is running"

        with patch("subprocess.run", return_value=mock_result):
            actions = _stop_container_stack(state, warnings)

        # Both stop and rm fail
        assert len(warnings) == 2
        assert "failed" in warnings[0].lower()
        assert "failed" in warnings[1].lower()
        assert actions[0]["action"] == "container_stop_failed"
        assert actions[1]["action"] == "container_rm_failed"

    def test_container_stop_timeout(self, tmp_path: Path):
        """subprocess timeout on stop and rm adds warnings."""
        state: dict[str, Any] = {
            "deployment": "container",
            "runtime_binary": "podman",
        }
        warnings: list[str] = []

        with patch(
            "subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="timeout", timeout=60)
        ):
            actions = _stop_container_stack(state, warnings)

        # Both stop and rm timeout
        assert len(warnings) == 2
        assert "timed out" in warnings[0].lower()
        assert "timed out" in warnings[1].lower()
        assert actions[0]["action"] == "container_stop_timeout"
        assert actions[1]["action"] == "container_rm_timeout"

    def test_remove_container_image_success(self, tmp_path: Path):
        """Container image is removed when deployment='container'."""
        state: dict[str, Any] = {
            "deployment": "container",
            "runtime_binary": "podman",
            "image_tag": "ghcr.io/nrmeyers/agentalloy:latest",
        }
        warnings: list[str] = []

        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            actions = _remove_container_image(state, warnings)

        assert len(actions) == 1
        assert actions[0]["action"] == "image_removed"
        assert actions[0]["image"] == "ghcr.io/nrmeyers/agentalloy:latest"
        mock_run.assert_called_once_with(
            ["podman", "rmi", "-f", "ghcr.io/nrmeyers/agentalloy:latest"],
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_remove_container_image_skips_native(self):
        """Container image removal is skipped for non-container deployments."""
        state: dict[str, Any] = {"deployment": "native"}
        warnings: list[str] = []
        actions = _remove_container_image(state, warnings)
        assert actions == []

    def test_remove_container_image_missing_binary_warns(self):
        """Missing runtime binary produces a warning and empty actions."""
        state: dict[str, Any] = {
            "deployment": "container",
            "runtime_binary": None,
        }
        warnings: list[str] = []
        actions = _remove_container_image(state, warnings)
        assert actions == []
        assert len(warnings) == 1
        assert "runtime binary unresolved" in warnings[0]

    def test_remove_container_image_none_tag_defaults(self):
        """None or missing image_tag falls back to ghcr.io/nrmeyers/agentalloy:latest."""
        state: dict[str, Any] = {
            "deployment": "container",
            "runtime_binary": "podman",
            "image_tag": None,
        }
        warnings: list[str] = []

        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            actions = _remove_container_image(state, warnings)

        assert len(actions) == 1
        assert actions[0]["action"] == "image_removed"
        assert actions[0]["image"] == "ghcr.io/nrmeyers/agentalloy:latest"
        mock_run.assert_called_once_with(
            ["podman", "rmi", "-f", "ghcr.io/nrmeyers/agentalloy:latest"],
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_remove_container_image_missing_key_defaults(self):
        """Missing image_tag key falls back to ghcr.io/nrmeyers/agentalloy:latest."""
        state: dict[str, Any] = {
            "deployment": "container",
            "runtime_binary": "podman",
        }
        warnings: list[str] = []

        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch("subprocess.run", return_value=mock_result):
            actions = _remove_container_image(state, warnings)

        assert len(actions) == 1
        assert actions[0]["action"] == "image_removed"
        assert actions[0]["image"] == "ghcr.io/nrmeyers/agentalloy:latest"

    def test_remove_agentalloy_cache_success(self, tmp_path: Path):
        """AgentAlloy cache directory is removed when it exists."""
        warnings: list[str] = []
        cache_dir = tmp_path / ".cache" / "agentalloy"
        cache_dir.mkdir(parents=True)

        with patch("pathlib.Path.home", return_value=tmp_path):
            actions = _remove_agentalloy_cache(warnings)

        assert len(actions) == 1
        assert actions[0]["action"] == "cache_removed"
        assert not cache_dir.exists()

    def test_remove_agentalloy_cache_already_gone(self, tmp_path: Path):
        """Missing AgentAlloy cache returns 'already_gone' action."""
        warnings: list[str] = []

        with patch("pathlib.Path.home", return_value=tmp_path):
            actions = _remove_agentalloy_cache(warnings)

        assert len(actions) == 1
        assert actions[0]["action"] == "cache_already_gone"
        assert len(warnings) == 0


class TestSentinelHelpers:
    """Test sentinel block extraction and removal."""

    def test_extract_sentinel_content_found(self):
        text = "before\n<!-- BEGIN AGENTALLOY -->\nsome content\n<!-- END AGENTALLOY -->\nafter"
        result = _extract_sentinel_content(
            text, "<!-- BEGIN AGENTALLOY -->", "<!-- END AGENTALLOY -->"
        )
        assert result == "some content"

    def test_extract_sentinel_content_not_found(self):
        text = "no sentinels here"
        result = _extract_sentinel_content(text, "BEGIN", "END")
        assert result is None

    def test_extract_sentinel_only_begin_missing(self):
        """When markers are reversed (END before BEGIN), returns empty string."""
        text = "has END but no BEGIN"
        result = _extract_sentinel_content(text, "BEGIN", "END")
        # Both "BEGIN" and "END" are substrings, but END comes before BEGIN,
        # so the extraction range is reversed and returns empty string.
        assert result == ""

    def test_remove_sentinel_block(self):
        text = "before\n\n<!-- BEGIN AGENTALLOY -->\nsome content\n<!-- END AGENTALLOY -->\nafter"
        result = _remove_sentinel_block(
            text, "<!-- BEGIN AGENTALLOY -->", "<!-- END AGENTALLOY -->"
        )
        assert "some content" not in result
        assert "before" in result
        assert "after" in result

    def test_remove_sentinel_block_not_found(self):
        text = "no sentinels here"
        result = _remove_sentinel_block(text, "BEGIN", "END")
        assert result == text

    def test_remove_sentinel_clean_double_blanks(self):
        text = "before\n\n<!-- BEGIN -->\ncontent\n<!-- END -->\n\n\n\nafter"
        result = _remove_sentinel_block(text, "<!-- BEGIN -->", "<!-- END -->")
        # Should clean up triple+ newlines
        assert "\n\n\n" not in result


class TestRemovePulledModels:
    """Test _remove_pulled_models helper."""

    def test_no_models_pulled(self):
        from agentalloy.install.subcommands.uninstall import _remove_pulled_models  # type: ignore[attr-defined]

        actions = _remove_pulled_models({})
        assert actions == []

    def test_malformed_entry_skipped(self):
        from agentalloy.install.subcommands.uninstall import _remove_pulled_models  # type: ignore[attr-defined]

        actions = _remove_pulled_models({"models_pulled": [123, None, ""]})
        for action in actions:
            assert action["action"] in ("skipped_malformed_entry", "skipped_empty_fields")

    def test_unmanaged_runner_skipped(self):
        from agentalloy.install.subcommands.uninstall import _remove_pulled_models  # type: ignore[attr-defined]

        actions = _remove_pulled_models({"models_pulled": ["lm-studio:some-model"]})
        assert len(actions) == 1
        assert actions[0]["action"] == "skipped_unmanaged_runner"


class TestDetectInstallMode:
    """Test _detect_install_mode detection logic."""

    @patch("agentalloy.install.subcommands.uninstall.shutil.which")
    @patch("agentalloy.install.subcommands.uninstall.subprocess.run")
    def test_uv_tool_mode_detected(self, mock_run: MagicMock, mock_which: MagicMock):
        """uv tool list contains agentalloy -> mode is uv_tool."""
        mock_which.side_effect = _which_map(
            uv="/usr/bin/uv", agentalloy="/usr/local/bin/agentalloy"
        )
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "agentalloy 1.0.0 /path/to/venv\n"
        mock_run.return_value = mock_result

        from agentalloy.install.subcommands.uninstall import _detect_install_mode  # type: ignore[attr-defined]

        result = _detect_install_mode()
        assert result["mode"] == "uv_tool"
        assert result["binary_path"] == "/usr/local/bin/agentalloy"
        assert result["venv_path"] is None
        assert "uv tool" in result["details"]
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == "/usr/bin/uv"
        assert "tool" in call_args
        assert "list" in call_args

    @patch("agentalloy.install.subcommands.uninstall.shutil.which")
    @patch("agentalloy.install.subcommands.uninstall.subprocess.run")
    def test_pipx_mode_detected(self, mock_run: MagicMock, mock_which: MagicMock):
        """uv tool list does NOT contain agentalloy, pipx does -> mode is pipx."""
        mock_which.side_effect = _which_map(
            uv="/usr/bin/uv", pipx="/usr/bin/pipx", agentalloy="/usr/bin/agentalloy"
        )

        # First call: uv tool list — no agentalloy
        uv_result = MagicMock()
        uv_result.returncode = 0
        uv_result.stdout = "some-other-tool 1.0.0 /path\n"

        # Second call: pipx list --short — agentalloy found
        pipx_result = MagicMock()
        pipx_result.returncode = 0
        pipx_result.stdout = "agentalloy 1.0.0\n"

        mock_run.side_effect = [uv_result, pipx_result]

        from agentalloy.install.subcommands.uninstall import _detect_install_mode  # type: ignore[attr-defined]

        result = _detect_install_mode()
        assert result["mode"] == "pipx"
        assert "pipx" in result["details"].lower()

    @patch("agentalloy.install.subcommands.uninstall.shutil.which")
    @patch("agentalloy.install.subcommands.uninstall.subprocess.run")
    def test_editable_mode_detected(
        self, mock_run: MagicMock, mock_which: MagicMock, tmp_path: Path
    ):
        """Binary under .venv + pyproject.toml with name=agentalloy -> mode is editable."""
        # Set up .venv and pyproject.toml
        venv_dir = tmp_path / ".venv"
        venv_dir.mkdir()
        bin_dir = venv_dir / "bin"
        bin_dir.mkdir()
        binary_path = str(bin_dir / "agentalloy")

        repo_root = tmp_path
        pyproject = repo_root / "pyproject.toml"
        pyproject.write_text('[project]\nname = "agentalloy"\n')

        mock_which.side_effect = _which_single("agentalloy", binary_path)

        # uv tool list returns no agentalloy (but uv is not even found)
        # pipx is not found either

        from agentalloy.install.subcommands.uninstall import _detect_install_mode  # type: ignore[attr-defined]

        result = _detect_install_mode()
        assert result["mode"] == "editable"
        assert result["venv_path"] == str(venv_dir)
        assert ".venv" in result["details"]

    @patch("agentalloy.install.subcommands.uninstall.shutil.which")
    def test_unknown_mode_detected(self, mock_which: MagicMock):
        """No detection method matches -> mode is unknown."""
        mock_which.side_effect = _which_none()

        from agentalloy.install.subcommands.uninstall import _detect_install_mode  # type: ignore[attr-defined]

        result = _detect_install_mode()
        assert result["mode"] == "unknown"
        assert result["binary_path"] is None
        assert result["venv_path"] is None
        assert "could not be determined" in result["details"].lower()

    @patch("agentalloy.install.subcommands.uninstall.shutil.which")
    @patch("agentalloy.install.subcommands.uninstall.subprocess.run")
    def test_uv_tool_list_timeout_falls_through(self, mock_run: MagicMock, mock_which: MagicMock):
        """subprocess.TimeoutExpired during uv tool list causes pipx check to run."""
        mock_which.side_effect = _which_map(
            uv="/usr/bin/uv", pipx="/usr/bin/pipx", agentalloy="/usr/bin/agentalloy"
        )

        # First call: uv tool list — timeout
        # Second call: pipx list --short — agentalloy found
        pipx_result = MagicMock()
        pipx_result.returncode = 0
        pipx_result.stdout = "agentalloy 1.0.0\n"

        mock_run.side_effect = [
            subprocess.TimeoutExpired(cmd=["uv", "tool", "list"], timeout=10),
            pipx_result,
        ]

        from agentalloy.install.subcommands.uninstall import _detect_install_mode  # type: ignore[attr-defined]

        result = _detect_install_mode()
        assert result["mode"] == "pipx"
        # uv tool list was called, pipx list was called as fallback
        assert mock_run.call_count == 2


class TestRemoveCliInstall:
    """Test _remove_cli_install dispatch and individual removal strategies."""

    @patch("agentalloy.install.subcommands.uninstall.shutil.which")
    @patch("agentalloy.install.subcommands.uninstall.subprocess.run")
    def test_uv_tool_mode_uninstalls(self, mock_run: MagicMock, mock_which: MagicMock):
        """uv_tool mode -> uv tool uninstall succeeds -> action uv_tool_uninstalled."""
        mock_which.side_effect = _which_single("uv", "/usr/bin/uv")
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_run.return_value = mock_result

        from agentalloy.install.subcommands.uninstall import _remove_cli_install  # type: ignore[attr-defined]

        mode_info = {"mode": "uv_tool", "binary_path": "/usr/bin/agentalloy"}
        result = _remove_cli_install(mode_info)
        assert result["action"] == "uv_tool_uninstalled"
        assert result["mode"] == "uv_tool"
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == "/usr/bin/uv"
        assert "tool" in call_args
        assert "uninstall" in call_args
        assert "agentalloy" in call_args

    @patch("agentalloy.install.subcommands.uninstall.shutil.which")
    @patch("agentalloy.install.subcommands.uninstall.subprocess.run")
    def test_pipx_mode_uninstalls(self, mock_run: MagicMock, mock_which: MagicMock):
        """pipx mode -> pipx uninstall succeeds -> action pipx_uninstalled."""
        mock_which.side_effect = _which_single("pipx", "/usr/bin/pipx")
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_run.return_value = mock_result

        from agentalloy.install.subcommands.uninstall import _remove_cli_install  # type: ignore[attr-defined]

        mode_info = {"mode": "pipx", "binary_path": "/usr/bin/agentalloy"}
        result = _remove_cli_install(mode_info)
        assert result["action"] == "pipx_uninstalled"
        assert result["mode"] == "pipx"
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == "/usr/bin/pipx"
        assert "uninstall" in call_args
        assert "agentalloy" in call_args

    def test_editable_mode_left_in_place(self):
        """editable mode -> action editable_install_left_in_place with venv_path."""
        from agentalloy.install.subcommands.uninstall import _remove_cli_install  # type: ignore[attr-defined]

        venv = "/home/user/project/.venv"
        mode_info = {
            "mode": "editable",
            "binary_path": "/home/user/project/.venv/bin/agentalloy",
            "venv_path": venv,
        }
        result = _remove_cli_install(mode_info)
        assert result["action"] == "editable_install_left_in_place"
        assert result["mode"] == "editable"
        assert result["venv_path"] == venv
        assert "Editable install" in result["details"]

    def test_unknown_mode_skipped(self):
        """unknown mode -> action cli_install_skipped."""
        from agentalloy.install.subcommands.uninstall import _remove_cli_install  # type: ignore[attr-defined]

        mode_info = {"mode": "unknown", "binary_path": None}
        result = _remove_cli_install(mode_info)
        assert result["action"] == "cli_install_skipped"
        assert result["mode"] == "unknown"
        assert "not found in PATH" in result["reason"]

    @patch("agentalloy.install.subcommands.uninstall.shutil.which")
    @patch("agentalloy.install.subcommands.uninstall.subprocess.run")
    def test_uv_tool_uninstall_fails(self, mock_run: MagicMock, mock_which: MagicMock):
        """uv tool uninstall returns non-zero -> action uv_tool_skipped with reason."""
        mock_which.side_effect = _which_single("uv", "/usr/bin/uv")
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "tool 'agentalloy' is not installed"
        mock_run.return_value = mock_result

        from agentalloy.install.subcommands.uninstall import _remove_cli_install  # type: ignore[attr-defined]

        mode_info = {"mode": "uv_tool", "binary_path": "/usr/bin/agentalloy"}
        result = _remove_cli_install(mode_info)
        assert result["action"] == "uv_tool_skipped"
        assert result["mode"] == "uv_tool"
        assert "not installed" in result["reason"]


class TestResultDictKeys:
    """Test that uninstall() returns the correct result dict keys."""

    @patch("agentalloy.install.server_proc.find_listening_pid", return_value=None)
    @patch("agentalloy.install.subcommands.uninstall._detect_install_mode")
    @patch("agentalloy.install.subcommands.uninstall.shutil.which")
    def test_cli_install_key_and_uv_tool_alias(
        self,
        mock_which: MagicMock,
        mock_detect: MagicMock,
        mock_find_pid: MagicMock,
        tmp_path: Path,
    ):
        """Result dict has 'cli_install' as primary key and 'uv_tool' as deprecated alias."""
        mock_which.side_effect = _which_none()
        mock_detect.return_value = {
            "mode": "unknown",
            "binary_path": None,
            "venv_path": None,
            "details": "Install mode could not be determined",
        }

        from agentalloy.install.subcommands.uninstall import uninstall

        minimal_state: dict[str, Any] = {
            "harness_files_written": [],
        }

        with (
            patch("agentalloy.install.state.load_state", return_value=minimal_state),
            patch("agentalloy.install.state.user_data_dir", return_value=tmp_path / "data"),
            patch("agentalloy.install.state.user_config_dir", return_value=tmp_path / "config"),
        ):
            result = uninstall(
                remove_data=False,
                force=True,
                stop_services=True,
                root=tmp_path,
            )

        # Primary key
        assert "cli_install" in result
        # Deprecated alias
        assert "uv_tool" in result
        # Both point to the same dict
        assert result["cli_install"] is result["uv_tool"]
        # install_mode is present
        assert "install_mode" in result

    @patch("agentalloy.install.server_proc.find_listening_pid", return_value=None)
    @patch("agentalloy.install.subcommands.uninstall._detect_install_mode")
    @patch("agentalloy.install.subcommands.uninstall.shutil.which")
    def test_cli_install_key_contains_action(
        self,
        mock_which: MagicMock,
        mock_detect: MagicMock,
        mock_find_pid: MagicMock,
        tmp_path: Path,
    ):
        """cli_install result contains an 'action' field."""
        mock_which.side_effect = _which_none()
        mock_detect.return_value = {
            "mode": "unknown",
            "binary_path": None,
            "venv_path": None,
            "details": "Install mode could not be determined",
        }

        from agentalloy.install.subcommands.uninstall import uninstall

        minimal_state: dict[str, Any] = {
            "harness_files_written": [],
        }

        with (
            patch("agentalloy.install.state.load_state", return_value=minimal_state),
            patch("agentalloy.install.state.user_data_dir", return_value=tmp_path / "data"),
            patch("agentalloy.install.state.user_config_dir", return_value=tmp_path / "config"),
        ):
            result = uninstall(
                remove_data=False,
                force=True,
                stop_services=True,
                root=tmp_path,
            )

        assert "action" in result["cli_install"]
        assert result["cli_install"]["action"] == "cli_install_skipped"


class TestPromptUninstallPreset:
    """Test _prompt_uninstall_preset interactive menu."""

    def test_default_is_full_bare_enter(self):
        """Bare Enter (empty string) returns 'full'."""
        from agentalloy.install.subcommands.uninstall import _prompt_uninstall_preset  # type: ignore[attr-defined]

        with patch("builtins.input", return_value=""):
            result = _prompt_uninstall_preset()
        assert result == "full"

    def test_default_is_full_eof(self):
        """EOFError (Ctrl-D / pipe) returns 'full'."""
        from agentalloy.install.subcommands.uninstall import _prompt_uninstall_preset  # type: ignore[attr-defined]

        with patch("builtins.input", side_effect=EOFError()):
            result = _prompt_uninstall_preset()
        assert result == "full"

    def test_choice_2_returns_keep_data(self):
        """Input '2' returns 'keep-data'."""
        from agentalloy.install.subcommands.uninstall import _prompt_uninstall_preset  # type: ignore[attr-defined]

        with patch("builtins.input", return_value="2"):
            result = _prompt_uninstall_preset()
        assert result == "keep-data"

    def test_choice_3_returns_custom(self):
        """Input '3' returns 'custom'."""
        from agentalloy.install.subcommands.uninstall import _prompt_uninstall_preset  # type: ignore[attr-defined]

        with patch("builtins.input", return_value="3"):
            result = _prompt_uninstall_preset()
        assert result == "custom"


class TestPortConflictDiagnostics:
    """Uninstall delegates process/unit/shim teardown to ``runtime_artifacts.reap``
    and maps its actions into the result. The foreign-vs-ours kill decision lives
    in runtime_artifacts (covered in test_runtime_artifacts.py); here we only check
    that uninstall consumes the actions: warn_foreign → warning (never recorded as
    removed), executed actions → files_removed entries."""

    def test_foreign_process_warns_no_kill(self, tmp_path: Path) -> None:
        """A warn_foreign action surfaces as a warning, not a removal."""
        from agentalloy.install.runtime_artifacts import Action
        from agentalloy.install.subcommands.uninstall import uninstall

        foreign = Action(
            "warn_foreign",
            "pid://12345",
            ":47950 held by foreign pid 12345 — left running",
            executed=False,
        )
        minimal_state: dict[str, Any] = {"harness_files_written": [], "port": 47950}

        with (
            patch("agentalloy.install.runtime_artifacts.reap", return_value=[foreign]) as mock_reap,
            patch("agentalloy.install.state.load_state", return_value=minimal_state),
            patch("agentalloy.install.state.user_data_dir", return_value=tmp_path / "data"),
            patch("agentalloy.install.state.user_config_dir", return_value=tmp_path / "config"),
        ):
            result = uninstall(remove_data=False, force=True, stop_services=True, root=tmp_path)

        mock_reap.assert_called_once_with("all")
        warnings = result.get("warnings", [])
        assert any("foreign pid 12345" in w for w in warnings), warnings
        # Advisory only — nothing recorded as removed for the foreign holder.
        files_removed = result.get("files_removed", [])
        assert not any(f.get("path") == "pid://12345" for f in files_removed), files_removed

    def test_agentalloy_process_stopped(self, tmp_path: Path) -> None:
        """An executed stop_process action is recorded in files_removed."""
        from agentalloy.install.runtime_artifacts import Action
        from agentalloy.install.subcommands.uninstall import uninstall

        stopped = Action(
            "stop_process",
            "pid://12345",
            "stopped uvicorn on :47950 (pid 12345)",
            executed=True,
        )
        minimal_state: dict[str, Any] = {"harness_files_written": [], "port": 47950}

        with (
            patch("agentalloy.install.runtime_artifacts.reap", return_value=[stopped]),
            patch("agentalloy.install.state.load_state", return_value=minimal_state),
            patch("agentalloy.install.state.user_data_dir", return_value=tmp_path / "data"),
            patch("agentalloy.install.state.user_config_dir", return_value=tmp_path / "config"),
        ):
            result = uninstall(remove_data=False, force=True, stop_services=True, root=tmp_path)

        files_removed = result.get("files_removed", [])
        assert any(
            f.get("path") == "pid://12345" and f.get("action") == "stop_process"
            for f in files_removed
        ), files_removed
