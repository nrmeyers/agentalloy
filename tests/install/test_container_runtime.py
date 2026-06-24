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


def _which_for(*present: str):
    """Return a shutil.which side_effect that reports only ``present`` binaries."""
    return lambda name: f"/usr/bin/{name}" if name in present else None


def _functional_only(*functional: str):
    """Return a _runtime_is_functional side_effect for the given working binaries."""
    return lambda binary: binary in functional


class TestDetectRuntimeBinary:
    """UT-1: _detect_runtime_binary() prefers a functional runtime, podman first."""

    def test_returns_podman_when_only_podman_on_path(self):
        """When only podman exists on PATH (and works), returns 'podman'."""
        with (
            patch.object(shutil, "which", side_effect=_which_for("podman")),
            patch.object(
                container_runtime, "_runtime_is_functional", side_effect=_functional_only("podman")
            ),
        ):
            assert container_runtime._detect_runtime_binary() == "podman"

    def test_returns_docker_when_only_docker_on_path(self):
        """When only docker exists on PATH (and works), returns 'docker'."""
        with (
            patch.object(shutil, "which", side_effect=_which_for("docker")),
            patch.object(
                container_runtime, "_runtime_is_functional", side_effect=_functional_only("docker")
            ),
        ):
            assert container_runtime._detect_runtime_binary() == "docker"

    def test_returns_none_when_neither_on_path(self):
        """When neither podman nor docker exists on PATH, returns None."""
        with (
            patch.object(shutil, "which", return_value=None),
            patch.object(container_runtime, "_runtime_is_functional", return_value=False),
        ):
            assert container_runtime._detect_runtime_binary() is None

    def test_priority_podman_over_docker_when_both_functional(self):
        """When both podman and docker work, returns 'podman' (preference)."""
        with (
            patch.object(shutil, "which", side_effect=_which_for("podman", "docker")),
            patch.object(container_runtime, "_runtime_is_functional", return_value=True),
        ):
            assert container_runtime._detect_runtime_binary() == "podman"

    def test_prefers_functional_docker_over_present_but_broken_podman(self):
        """Regression (macOS): podman CLI present but no machine, docker works → docker.

        This is the bug that picked podman by presence and ignored a working
        Docker Desktop.
        """
        with (
            patch.object(shutil, "which", side_effect=_which_for("podman", "docker")),
            patch.object(
                container_runtime, "_runtime_is_functional", side_effect=_functional_only("docker")
            ),
        ):
            assert container_runtime._detect_runtime_binary() == "docker"

    def test_falls_back_to_first_present_when_none_functional(self):
        """Both present but neither responds → first present (podman) for a useful error."""
        with (
            patch.object(shutil, "which", side_effect=_which_for("podman", "docker")),
            patch.object(container_runtime, "_runtime_is_functional", return_value=False),
        ):
            assert container_runtime._detect_runtime_binary() == "podman"


class TestDetectFunctionalRuntimes:
    """_detect_functional_runtimes(): present-and-working only, podman first."""

    def test_filters_out_present_but_nonfunctional(self):
        with (
            patch.object(shutil, "which", side_effect=_which_for("podman", "docker")),
            patch.object(
                container_runtime, "_runtime_is_functional", side_effect=_functional_only("docker")
            ),
        ):
            assert container_runtime._detect_functional_runtimes() == ["docker"]

    def test_returns_both_in_preference_order(self):
        with (
            patch.object(shutil, "which", side_effect=_which_for("podman", "docker")),
            patch.object(container_runtime, "_runtime_is_functional", return_value=True),
        ):
            assert container_runtime._detect_functional_runtimes() == ["podman", "docker"]

    def test_empty_when_none_present(self):
        with patch.object(shutil, "which", return_value=None):
            assert container_runtime._detect_functional_runtimes() == []


class TestRuntimeIsFunctional:
    """_runtime_is_functional(): `<binary> info` return code drives the verdict."""

    def test_true_on_exit_zero(self):
        with patch.object(subprocess, "run", return_value=MagicMock(returncode=0)) as run:
            assert container_runtime._runtime_is_functional("docker") is True
        assert run.call_args.args[0] == ["docker", "info"]

    def test_false_on_nonzero_exit(self):
        with patch.object(subprocess, "run", return_value=MagicMock(returncode=125)):
            assert container_runtime._runtime_is_functional("podman") is False

    def test_false_on_timeout(self):
        with patch.object(
            subprocess, "run", side_effect=subprocess.TimeoutExpired(cmd="podman info", timeout=15)
        ):
            assert container_runtime._runtime_is_functional("podman") is False

    def test_false_on_oserror(self):
        with patch.object(subprocess, "run", side_effect=OSError("boom")):
            assert container_runtime._runtime_is_functional("podman") is False


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


# ---------------------------------------------------------------------------
# _run_container bind-failure hint
# ---------------------------------------------------------------------------


class TestRunContainerBindHint:
    """The port-reservation hint must fire on the failure shape reality produces.

    _run_container's subprocess.run does NOT capture stderr (it streams to the
    user's terminal), so CalledProcessError.stderr is None — the hint keys off
    exit code 126 (observed rootlessport bind failure, 2026-06-10).
    """

    def test_rc_126_without_stderr_prints_hint(self, tmp_path: Path):
        err = subprocess.CalledProcessError(126, ["podman", "run"])  # stderr=None
        printed: list[str] = []
        with (
            patch(
                "agentalloy.install.subcommands.container_runtime.subprocess.run",
                side_effect=err,
            ),
            patch(
                "agentalloy.install.subcommands.container_runtime._print",
                side_effect=lambda msg: printed.append(str(msg)),
            ),
        ):
            rc = container_runtime._run_container("podman", "")
        assert rc == 126
        assert any("port" in p.lower() and "ps -a" in p for p in printed)

    def test_other_failure_rc_does_not_print_hint(self, tmp_path: Path):
        err = subprocess.CalledProcessError(1, ["podman", "run"])  # stderr=None
        printed: list[str] = []
        with (
            patch(
                "agentalloy.install.subcommands.container_runtime.subprocess.run",
                side_effect=err,
            ),
            patch(
                "agentalloy.install.subcommands.container_runtime._print",
                side_effect=lambda msg: printed.append(str(msg)),
            ),
        ):
            rc = container_runtime._run_container("podman", "")
        assert rc == 1
        assert not any("ps -a" in p for p in printed)


# ---------------------------------------------------------------------------
# UT-6: _list_conflicting_containers()
# ---------------------------------------------------------------------------


class TestListConflictingContainers:
    """UT-6: _list_conflicting_containers() merges name-match and port-match results."""

    def _make_proc(self, stdout: str, returncode: int = 0) -> MagicMock:
        m = MagicMock()
        m.returncode = returncode
        m.stdout = stdout
        return m

    def test_name_match_only_returns_container(self):
        """A container matching by name (no label, no port) is returned."""
        # name filter returns a hit; port filter returns nothing.
        responses = [
            self._make_proc("agentalloy\tExited (1) 2 minutes ago"),  # name filter
            self._make_proc(""),  # port filter
        ]
        with patch(
            "agentalloy.install.subcommands.container_runtime.subprocess.run",
            side_effect=responses,
        ):
            result = container_runtime._list_conflicting_containers("podman")
        assert result == [("agentalloy", "Exited (1) 2 minutes ago")]

    def test_port_match_only_returns_container(self):
        """A container matching by port only (different name) is returned."""
        responses = [
            self._make_proc(""),  # name filter — no match
            self._make_proc("some-other-container\tUp 5 seconds"),  # port filter
        ]
        with patch(
            "agentalloy.install.subcommands.container_runtime.subprocess.run",
            side_effect=responses,
        ):
            result = container_runtime._list_conflicting_containers("podman")
        assert result == [("some-other-container", "Up 5 seconds")]

    def test_dedup_when_container_matches_both_name_and_port(self):
        """A container returned by both strategies appears only once."""
        same_line = "agentalloy\tExited (1) 1 minute ago"
        responses = [
            self._make_proc(same_line),  # name filter
            self._make_proc(same_line),  # port filter — same container
        ]
        with patch(
            "agentalloy.install.subcommands.container_runtime.subprocess.run",
            side_effect=responses,
        ):
            result = container_runtime._list_conflicting_containers("podman")
        assert len(result) == 1
        assert result[0][0] == "agentalloy"

    def test_all_podman_failures_return_empty_list(self):
        """When every subprocess call fails, returns [] (setup proceeds)."""
        with patch(
            "agentalloy.install.subcommands.container_runtime.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["podman"], timeout=10),
        ):
            result = container_runtime._list_conflicting_containers("podman")
        assert result == []

    def test_partial_failure_returns_successful_results(self):
        """If name filter times out but port filter succeeds, port results are returned."""
        with patch(
            "agentalloy.install.subcommands.container_runtime.subprocess.run",
        ) as mock_run:
            mock_run.side_effect = [
                subprocess.TimeoutExpired(cmd=["podman"], timeout=10),  # name filter
                self._make_proc("agentalloy\tExited (137) 3 hours ago"),  # port filter
            ]
            result = container_runtime._list_conflicting_containers("podman")
        assert result == [("agentalloy", "Exited (137) 3 hours ago")]

    def test_lines_without_tab_delimiter_are_ignored(self):
        """Lines not containing a tab are not parsed (guards against test mock leakage)."""
        responses = [
            self._make_proc("agentalloy"),  # no tab — ignored
            self._make_proc(""),
        ]
        with patch(
            "agentalloy.install.subcommands.container_runtime.subprocess.run",
            side_effect=responses,
        ):
            result = container_runtime._list_conflicting_containers("podman")
        assert result == []

    def test_custom_container_name_and_port(self):
        """Custom container_name and port are passed through to subprocess filters."""
        calls: list[list[str]] = []

        def capture(cmd: list[str], **kwargs: object) -> MagicMock:
            calls.append(cmd)
            return self._make_proc("")

        with patch(
            "agentalloy.install.subcommands.container_runtime.subprocess.run",
            side_effect=capture,
        ):
            container_runtime._list_conflicting_containers(
                "podman", container_name="myapp", port=9999
            )

        # First call: name filter with custom name
        assert any("name=^myapp$" in arg for arg in calls[0])
        # Second call: port filter with custom port
        assert any("publish=9999" in arg for arg in calls[1])


# ---------------------------------------------------------------------------
# Projects-root bind mount: the proxy must see each repo's .agentalloy/ at the
# decoded host path, so _run_container bind-mounts the projects root rw there.
# ---------------------------------------------------------------------------


class TestProjectsRootMount:
    """_run_container bind-mounts resolve_projects_root() rw at its identical path."""

    def _run(self, tmp_path: Path, packs: str = "") -> list[str]:
        captured: list[str] = []

        def _fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
            if cmd[:2] == ["podman", "run"]:
                captured.clear()
                captured.extend(cmd)
            return MagicMock(returncode=0)

        with patch(
            "agentalloy.install.subcommands.container_runtime.subprocess.run",
            side_effect=_fake_run,
        ):
            rc = container_runtime._run_container("podman", packs)
        assert rc == 0
        return captured

    def test_env_root_mounted_rw_at_identical_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "code"
        root.mkdir()
        monkeypatch.setenv("AGENTALLOY_PROJECTS_ROOT", str(root))
        cmd = self._run(tmp_path)
        assert f"{root}:{root}:rw" in cmd

    def test_root_filesystem_is_not_mounted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENTALLOY_PROJECTS_ROOT", "/")
        cmd = self._run(tmp_path)
        assert not any(arg == "/:/:rw" for arg in cmd)
        assert "/:/:rw" not in cmd

    def test_resolve_projects_root_defaults_to_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AGENTALLOY_PROJECTS_ROOT", raising=False)
        import os

        assert container_runtime.resolve_projects_root() == Path(os.path.realpath(Path.home()))


# ---------------------------------------------------------------------------
# Restartable container: the baked /app/entrypoint.sh runs (no host bind-mount),
# packs are delivered via the AGENTALLOY_PACKS env var. A host-generated
# entrypoint bind-mount source is deleted after install, which broke
# `podman start agentalloy` / reboot (the declared --restart unless-stopped).
# ---------------------------------------------------------------------------


class TestRestartableEntrypoint:
    """_run_container must NOT bind-mount a host entrypoint; packs go via env."""

    def _argv(self, packs: str, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        # Pin the projects root so the mount line is deterministic.
        monkeypatch.delenv("AGENTALLOY_PROJECTS_ROOT", raising=False)
        captured: list[str] = []

        def _fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
            if cmd[:2] == ["podman", "run"]:
                captured.clear()
                captured.extend(cmd)
            return MagicMock(returncode=0)

        with patch(
            "agentalloy.install.subcommands.container_runtime.subprocess.run",
            side_effect=_fake_run,
        ):
            rc = container_runtime._run_container("podman", packs)
        assert rc == 0
        return captured

    def test_no_entrypoint_bind_mount(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cmd = self._argv("rust,python", monkeypatch)
        # No host-generated entrypoint bind-mount and no /app/entrypoint.sh arg
        # override — the image's baked ENTRYPOINT/CMD runs instead.
        assert not any(":/app/entrypoint.sh:ro" in arg for arg in cmd)
        assert "/app/entrypoint.sh" not in cmd

    def test_packs_passed_via_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cmd = self._argv("rust,python", monkeypatch)
        # -e AGENTALLOY_PACKS=rust,python must be present as adjacent argv pair.
        assert "-e" in cmd
        assert "AGENTALLOY_PACKS=rust,python" in cmd
        idx = cmd.index("AGENTALLOY_PACKS=rust,python")
        assert cmd[idx - 1] == "-e"

    def test_empty_packs_still_sets_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Empty packs still passes the (empty) env var; the baked entrypoint
        # falls back to always-on packs when AGENTALLOY_PACKS is empty.
        cmd = self._argv("", monkeypatch)
        assert "AGENTALLOY_PACKS=" in cmd

    def test_restart_policy_preserved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cmd = self._argv("", monkeypatch)
        idx = cmd.index("--restart")
        assert cmd[idx + 1] == "unless-stopped"
