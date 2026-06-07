"""Tests for container_runtime.py — runtime detection and image pull.

UT-1: _detect_runtime_binary() returns podman/docker/None based on PATH
UT-1: priority order is podman > docker > None
UT-2: _pull_image() pulls from GHCR in online mode
UT-2: _pull_image() loads from tarball in offline mode
UT-2: _pull_image() returns non-zero on failure
UT-2: _pull_image() returns non-zero on timeout
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
# UT-2: _pull_image()
# ---------------------------------------------------------------------------


class TestPullImage:
    """UT-2: _pull_image() pulls from GHCR in online mode."""

    def test_pulls_from_ghcr_by_default(self):
        """Default pull uses ghcr.io/nrmeyers/agentalloy:latest."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            container_runtime._pull_image("podman")
            cmd = mock_run.call_args[0][0]
            assert cmd == ["podman", "pull", "ghcr.io/nrmeyers/agentalloy:latest"]

    def test_pulls_custom_image_ref(self):
        """A custom image_ref is passed to the pull command."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            container_runtime._pull_image("docker", image_ref="myrepo/myimage:v1")
            cmd = mock_run.call_args[0][0]
            assert cmd == ["docker", "pull", "myrepo/myimage:v1"]

    def test_returns_zero_on_success(self):
        """Returns 0 when the pull succeeds."""
        with patch("subprocess.run", return_value=MagicMock(returncode=0)):
            result = container_runtime._pull_image("podman")
            assert result == 0

    def test_returns_nonzero_on_failure(self):
        """Returns non-zero exit code when the pull fails."""
        exc = subprocess.CalledProcessError(
            1, ["podman", "pull", "ghcr.io/nrmeyers/agentalloy:latest"]
        )
        exc.stderr = b"pull error"

        with patch("subprocess.run", side_effect=exc):
            result = container_runtime._pull_image("podman")
            assert result == 1

    def test_returns_nonzero_on_timeout(self):
        """Returns 1 when the pull times out after 600s."""
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("podman pull", 600)):
            result = container_runtime._pull_image("podman")
            assert result == 1

    def test_offline_load_from_tarball(self, tmp_path: Path):
        """Offline mode loads from a tarball."""
        tarball = tmp_path / "image.tar"
        tarball.write_bytes(b"fake")

        def _subprocess_run(cmd, **kwargs):
            mock = MagicMock()
            mock.returncode = 0
            mock.stdout = "ghcr.io/nrmeyers/agentalloy:latest\n"
            mock.stderr = b""
            return mock

        with patch("subprocess.run", side_effect=_subprocess_run):
            result = container_runtime._pull_image("podman", offline=True, tarball_path=tarball)
            assert result == 0

    def test_offline_missing_tarball_returns_1(self, tmp_path: Path):
        """Offline mode with missing tarball returns 1."""
        missing = tmp_path / "nonexistent.tar"
        result = container_runtime._pull_image("podman", offline=True, tarball_path=missing)
        assert result == 1


# ---------------------------------------------------------------------------
# UT-4: Offline image loading
# ---------------------------------------------------------------------------


class TestOfflineLoad:
    """UT-4: Tests for offline image loading via --image-path flag."""

    def test_load_from_tarball(self, tmp_path: Path):
        """Offline mode loads image from tarball via podman load."""
        tarball = tmp_path / "image.tar"
        tarball.write_bytes(b"fake-tarball")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="ghcr.io/nrmeyers/agentalloy:latest\n"
            )
            result = container_runtime._pull_image("podman", offline=True, tarball_path=tarball)
            assert result == 0
            # First call is podman load; verify the load command
            load_call = mock_run.call_args_list[0]
            assert load_call[0][0] == ["podman", "load", "-i", str(tarball)]
            assert load_call[1] == {"check": True, "capture_output": True, "timeout": 300}

    def test_offline_missing_tarball(self, tmp_path: Path):
        """Returns 1 when tarball does not exist."""
        missing = tmp_path / "nonexistent.tar"
        result = container_runtime._pull_image("podman", offline=True, tarball_path=missing)
        assert result == 1

    def test_offline_load_failure(self, tmp_path: Path):
        """Returns non-zero on podman load failure."""
        tarball = tmp_path / "image.tar"
        tarball.write_bytes(b"fake-tarball")
        exc = subprocess.CalledProcessError(1, ["podman", "load"])
        exc.stderr = b"invalid image format"
        with patch("subprocess.run", side_effect=exc):
            result = container_runtime._pull_image("podman", offline=True, tarball_path=tarball)
            assert result == 1

    def test_offline_timeout(self, tmp_path: Path):
        """Returns 1 on load timeout."""
        tarball = tmp_path / "image.tar"
        tarball.write_bytes(b"fake-tarball")
        with patch(
            "subprocess.run", side_effect=subprocess.TimeoutExpired(["podman", "load"], 300)
        ):
            result = container_runtime._pull_image("podman", offline=True, tarball_path=tarball)
            assert result == 1


# ---------------------------------------------------------------------------
# UT-5: Online pull failure scenarios
# ---------------------------------------------------------------------------


class TestPullImageFailureScenarios:
    """UT-5: Tests for online image pull failure scenarios."""

    def test_network_timeout(self):
        """Returns 1 when pull times out."""
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(
                ["podman", "pull", "ghcr.io/nrmeyers/agentalloy:latest"], 600
            ),
        ):
            result = container_runtime._pull_image("podman")
            assert result == 1

    def test_image_not_found(self):
        """Returns non-zero when image does not exist on GHCR."""
        exc = subprocess.CalledProcessError(125, ["podman", "pull"])
        exc.stderr = b"manifest unknown"
        with patch("subprocess.run", side_effect=exc):
            result = container_runtime._pull_image("podman")
            assert result == 125

    def test_custom_image_ref(self):
        """Uses custom image_ref when provided."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            container_runtime._pull_image(
                "podman", image_ref="ghcr.io/nrmeyers/agentalloy@sha256:abc123"
            )
            cmd = mock_run.call_args[0][0]
            assert cmd == ["podman", "pull", "ghcr.io/nrmeyers/agentalloy@sha256:abc123"]

    def test_default_image_is_ghcr(self):
        """Default image is ghcr.io/nrmeyers/agentalloy:latest."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            container_runtime._pull_image("podman")
            cmd = mock_run.call_args[0][0]
            assert cmd == ["podman", "pull", "ghcr.io/nrmeyers/agentalloy:latest"]


# ---------------------------------------------------------------------------
# UT-3: _ensure_volume()
# ---------------------------------------------------------------------------


class TestEnsureVolume:
    """UT-3: _ensure_volume() creates the data volume."""

    def test_creates_volume(self):
        """Creates agentalloy-data volume."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            container_runtime._ensure_volume("podman")
            cmd = mock_run.call_args[0][0]
            assert cmd == ["podman", "volume", "create", "agentalloy-data"]

    def test_silently_ignores_already_exists(self):
        """Already-exists error is silently ignored."""
        exc = subprocess.CalledProcessError(1, ["podman", "volume", "create", "agentalloy-data"])
        exc.stderr = b"volume already exists"

        with patch("subprocess.run", side_effect=exc):
            container_runtime._ensure_volume("podman")  # should not raise

    def test_raises_on_other_errors(self):
        """Other errors are re-raised."""
        exc = subprocess.CalledProcessError(1, ["podman", "volume", "create", "agentalloy-data"])
        exc.stderr = b"permission denied"

        with patch("subprocess.run", side_effect=exc):
            with pytest.raises(subprocess.CalledProcessError):
                container_runtime._ensure_volume("podman")


# ---------------------------------------------------------------------------
# UT-4: _ensure_ollama_dir()
# ---------------------------------------------------------------------------


class TestEnsureOllamaDir:
    """UT-4: _ensure_ollama_dir() creates ~/.ollama."""

    def test_creates_ollama_dir(self, tmp_path: Path):
        """Creates ~/.ollama directory."""
        fake_home = tmp_path / "home"
        with patch.object(Path, "home", return_value=fake_home):
            container_runtime._ensure_ollama_dir()
            assert (fake_home / ".ollama").is_dir()

    def test_idempotent_when_exists(self, tmp_path: Path):
        """No error if ~/.ollama already exists."""
        fake_home = tmp_path / "home"
        fake_home.mkdir(parents=True)
        (fake_home / ".ollama").mkdir()
        with patch.object(Path, "home", return_value=fake_home):
            container_runtime._ensure_ollama_dir()  # should not raise


# ---------------------------------------------------------------------------
# UT-5: _generate_entrypoint()
# ---------------------------------------------------------------------------


class TestEntrypoint:
    """UT-5: _generate_entrypoint() creates a valid bash script."""

    def test_creates_script(self, tmp_path: Path):
        """Returns a path to a file containing a bash script."""
        # _generate_entrypoint creates a real NamedTemporaryFile, writes to it,
        # and returns a Path. We verify the returned path exists and is executable.
        result = container_runtime._generate_entrypoint("rust,python")
        assert result.exists()
        assert result.stat().st_mode & 0o777 == 0o700
        result.unlink()  # clean up

    def test_script_contains_pack_names(self, tmp_path: Path):
        """Generated script references the requested packs."""
        result = container_runtime._generate_entrypoint("rust,python")
        content = result.read_text()
        assert "rust" in content
        assert "python" in content
        result.unlink()  # clean up
