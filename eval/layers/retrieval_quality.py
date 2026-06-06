"""Tests for container_runtime.py — runtime detection and build context location.

UT-1: _detect_runtime_binary() returns podman/docker/None based on PATH
UT-1: priority order is podman > docker > None
UT-2: _locate_build_context() finds compose.yaml + Containerfile in cwd
UT-2: _locate_build_context() falls back to parents[4] of __file__
UT-2: _locate_build_context() returns None when all strategies fail
UT-3: _build_image() constructs correct command
UT-3: _build_image() uses correct image tag and dockerfile
UT-3: _build_image() returns non-zero on failure
UT-3: _build_image() writes log on failure
UT-3: _build_image() has 600s timeout
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agentalloy.install.subcommands import container_runtime

# ---------------------------------------------------------------------------
# UT-1: _detect_runtime_binary()
# ---------------------------------------------------------------------------


class TestDetectRuntimeBinary:
    """UT-1: _detect_runtime_binary() returns podman/docker/None based on PATH."""

    def test_returns_podman_when_only_podman_on_path(self):
        """When only podman exists on PATH, returns 'podman'."""
        with patch.object(shutil, "which") as mock_which:
            mock_which.side_effect = lambda x: "podman" if x == "podman" else None
            result = container_runtime._detect_runtime_binary()
            assert result == "podman"

    def test_returns_docker_when_only_docker_on_path(self):
        """When only docker exists on PATH, returns 'docker'."""
        with patch.object(shutil, "which") as mock_which:
            mock_which.side_effect = lambda x: "docker" if x == "docker" else None
            result = container_runtime._detect_runtime_binary()
            assert result == "docker"

    def test_returns_none_when_neither_on_path(self):
        """When neither podman nor docker exists on PATH, returns None."""
        with patch.object(shutil, "which", return_value=None):
            result = container_runtime._detect_runtime_binary()
            assert result is None

    def test_priority_podman_over_docker(self):
        """When both podman and docker exist, returns 'podman' (priority)."""
        with patch.object(shutil, "which", return_value="/usr/bin/fake"):
            result = container_runtime._detect_runtime_binary()
            assert result == "podman"

    def test_calls_which_in_order_podman_then_docker(self):
        """Verifies the search order: podman first, then docker."""
        call_order = []

        def mock_which(name):
            call_order.append(name)
            return None

        with patch.object(shutil, "which", side_effect=mock_which):
            container_runtime._detect_runtime_binary()

        assert call_order == ["podman", "docker"]


# ---------------------------------------------------------------------------
# UT-2: _locate_build_context()
# ---------------------------------------------------------------------------


class TestLocateBuildContext:
    """UT-2: _locate_build_context() finds compose.yaml + Containerfile in cwd."""

    @pytest.fixture
    def _mock_has_assets(self, tmp_path: Path):
        """Create a directory with compose.yaml and Containerfile."""
        assets_dir = tmp_path / "build_context"
        assets_dir.mkdir()
        (assets_dir / "compose.yaml").write_text("services: {}\n")
        (assets_dir / "Containerfile").write_text("FROM python:3.11\n")
        return assets_dir

    def test_finds_in_cwd_when_assets_present(self, _mock_has_assets):
        """When cwd has compose.yaml + Containerfile, returns cwd/compose.yaml."""
        cwd = _mock_has_assets
        with patch("pathlib.Path.cwd", return_value=cwd):
            result = container_runtime._locate_build_context()
            assert result == cwd / "compose.yaml"

    def test_finds_dockerfile_as_alternative_to_containerfile(self, tmp_path: Path):
        """Containerfile or Dockerfile both count as build file."""
        assets_dir = tmp_path / "build_context"
        assets_dir.mkdir()
        (assets_dir / "compose.yaml").write_text("services: {}\n")
        (assets_dir / "Dockerfile").write_text("FROM python:3.11\n")

        with patch("pathlib.Path.cwd", return_value=assets_dir):
            result = container_runtime._locate_build_context()
            assert result == assets_dir / "compose.yaml"

    def test_returns_none_when_no_assets_in_cwd(self, tmp_path: Path):
        """When cwd has no compose.yaml + Containerfile, falls through."""
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()

        # Patch _has_assets to always return False (simulate no assets anywhere)
        with (
            patch("pathlib.Path.cwd", return_value=empty_dir),
            patch.object(container_runtime, "_has_assets", return_value=False),
        ):
            result = container_runtime._locate_build_context()
            assert result is None

    def test_returns_none_when_all_strategies_fail(self, tmp_path: Path):
        """When cwd, editable_root, and auto-clone all fail, returns None."""
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()

        with (
            patch("pathlib.Path.cwd", return_value=empty_dir),
            patch.object(container_runtime, "_has_assets", return_value=False),
        ):
            result = container_runtime._locate_build_context()
            assert result is None

    def test_falls_back_to_editable_root_when_cwd_has_no_assets(self, tmp_path: Path):
        """When cwd lacks assets but parents[4] of __file__ has them, uses that.

        We test the parents[4] logic by creating a fake module path where
        parents[4] resolves to the assets directory, then patching __file__.
        """
        # Build a fake module path where parents[4] = assets_dir
        # parents[0]=subcommands, [1]=install, [2]=agentalloy, [3]=src, [4]=fake
        assets_dir = tmp_path / "fake"
        assets_dir.mkdir(parents=True)
        (assets_dir / "compose.yaml").write_text("services: {}\n")
        (assets_dir / "Containerfile").write_text("FROM python:3.11\n")

        fake_module = (
            assets_dir / "src" / "agentalloy" / "install" / "subcommands" / "container_runtime.py"
        )
        fake_module.parent.mkdir(parents=True)

        with (
            patch("pathlib.Path.cwd", return_value=tmp_path / "empty_cwd"),
            patch.object(container_runtime, "_has_assets", side_effect=lambda d: d == assets_dir),
        ):
            original_file = container_runtime.__file__
            container_runtime.__file__ = str(fake_module)

            try:
                result = container_runtime._locate_build_context()
                assert result == assets_dir / "compose.yaml"
            finally:
                container_runtime.__file__ = original_file

    def test_uses_auto_clone_when_cwd_and_editable_fail(self, tmp_path: Path):
        """When cwd and editable root lack assets, tries auto-clone.

        Since _ensure_cached_repo is a nested function, we mock shutil.which
        (to skip git check) and subprocess.run (to simulate successful clone),
        plus mock _has_assets to skip cwd and editable root but validate
        the cached repo path.
        """
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()

        # _has_assets is called for cwd, editable_root, and cached_repo.
        # We return False for cwd and editable_root (real paths), True for cache.
        def selective_has_assets(d):
            # cwd (empty_dir) should fail
            if str(d) == str(empty_dir):
                return False
            # The real repo path exists on disk, so skip it
            if "dev/agentalloy" in str(d):
                return False
            # The cache dir from auto-clone should have assets
            return ".cache/agentalloy/repo" in str(d)

        with (
            patch("pathlib.Path.cwd", return_value=empty_dir),
            patch.object(container_runtime, "_has_assets", side_effect=selective_has_assets),
            patch("shutil.which", return_value="/usr/bin/git"),
            patch("subprocess.run", return_value=MagicMock(returncode=0)),
            patch("pathlib.Path.home", return_value=tmp_path),
        ):
            result = container_runtime._locate_build_context()
            # Should find the cloned repo at ~/.cache/agentalloy/repo
            assert str(tmp_path) in str(result)
            assert "compose.yaml" in str(result)


# ---------------------------------------------------------------------------
# UT-3: _build_image()
# ---------------------------------------------------------------------------


class TestBuildImage:
    """UT-3: _build_image() constructs correct command."""

    def test_constructs_correct_command(self, tmp_path: Path):
        """The command should be [runtime, build, -t, agentalloy:local, -f, Containerfile, context]."""
        context = tmp_path / "build_context"
        context.mkdir()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            container_runtime._build_image("podman", context)

            mock_run.assert_called_once_with(
                [
                    "podman",
                    "build",
                    "-t",
                    "agentalloy:local",
                    "-f",
                    "Containerfile",
                    str(context),
                ],
                check=True,
                timeout=600,
                capture_output=True,
            )

    def test_uses_correct_image_tag(self, tmp_path: Path):
        """The image tag should be 'agentalloy:local'."""
        context = tmp_path / "build_context"
        context.mkdir()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            container_runtime._build_image("docker", context)

            cmd = mock_run.call_args[0][0]
            assert "-t" in cmd
            tag_idx = cmd.index("-t")
            assert cmd[tag_idx + 1] == "agentalloy:local"

    def test_uses_correct_dockerfile(self, tmp_path: Path):
        """The dockerfile should be 'Containerfile'."""
        context = tmp_path / "build_context"
        context.mkdir()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            container_runtime._build_image("podman", context)

            cmd = mock_run.call_args[0][0]
            assert "-f" in cmd
            df_idx = cmd.index("-f")
            assert cmd[df_idx + 1] == "Containerfile"

    def test_returns_zero_on_success(self, tmp_path: Path):
        """Returns 0 when the build succeeds."""
        context = tmp_path / "build_context"
        context.mkdir()

        with patch("subprocess.run", return_value=MagicMock(returncode=0)):
            result = container_runtime._build_image("podman", context)
            assert result == 0

    def test_returns_nonzero_on_failure(self, tmp_path: Path):
        """Returns non-zero exit code when the build fails."""
        context = tmp_path / "build_context"
        context.mkdir()
        expected_rc = 1

        exc = subprocess.CalledProcessError(expected_rc, ["podman", "build"])
        exc.output = b"build output"
        exc.stderr = b"build error"

        with patch("subprocess.run", side_effect=exc):
            result = container_runtime._build_image("podman", context)
            assert result == expected_rc

    def test_writes_log_on_failure(self, tmp_path: Path):
        """On failure, writes captured build output to a log file."""
        context = tmp_path / "build_context"
        context.mkdir()

        exc = subprocess.CalledProcessError(1, ["podman", "build"])
        exc.output = b"build stdout"
        exc.stderr = b"build stderr"

        with patch("subprocess.run", side_effect=exc):
            with patch("tempfile.gettempdir", return_value=str(tmp_path)):
                container_runtime._build_image("podman", context)

                # Check that a log file was written
                log_files = list(tmp_path.glob("agentalloy-build.log"))
                assert len(log_files) == 1

                log_content = log_files[0].read_text()
                assert "exit 1" in log_content
                assert "build stdout" in log_content
                assert "build stderr" in log_content

    def test_has_600s_timeout(self, tmp_path: Path):
        """The subprocess call should have a 600-second timeout."""
        context = tmp_path / "build_context"
        context.mkdir()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            container_runtime._build_image("podman", context)

            call_kwargs = mock_run.call_args[1]
            assert call_kwargs.get("timeout") == 600

    def test_returns_nonzero_on_timeout(self, tmp_path: Path):
        """Returns 1 when the build times out after 600s."""
        context = tmp_path / "build_context"
        context.mkdir()

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("podman build", 600)):
            result = container_runtime._build_image("podman", context)
            assert result == 1
