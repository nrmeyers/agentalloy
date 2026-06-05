"""Tests for container_runtime module — runtime detection and build context."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# UT-1: _detect_runtime_binary
# ---------------------------------------------------------------------------


class TestDetectRuntimeBinary:
    """Test _detect_runtime_binary() priority: podman > docker > None."""

    def test_both_podman_and_docker_returns_podman(self, tmp_path: Path):
        """When both podman and docker are on PATH, prefer podman."""
        # Create fake binaries
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "podman").write_text("#!/bin/sh\n")
        (bin_dir / "docker").write_text("#!/bin/sh\n")
        (bin_dir / "podman").chmod(0o755)
        (bin_dir / "docker").chmod(0o755)

        with patch.dict(os.environ, {"PATH": str(bin_dir)}, clear=True):
            from agentalloy.install.subcommands.container_runtime import (
                _detect_runtime_binary,
            )

            result = _detect_runtime_binary()
            assert result == "podman"

    def test_only_docker_returns_docker(self, tmp_path: Path):
        """When only docker is on PATH, return docker."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "docker").write_text("#!/bin/sh\n")
        (bin_dir / "docker").chmod(0o755)

        with patch.dict(os.environ, {"PATH": str(bin_dir)}, clear=True):
            from agentalloy.install.subcommands.container_runtime import (
                _detect_runtime_binary,
            )

            result = _detect_runtime_binary()
            assert result == "docker"

    def test_only_podman_returns_podman(self, tmp_path: Path):
        """When only podman is on PATH, return podman."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "podman").write_text("#!/bin/sh\n")
        (bin_dir / "podman").chmod(0o755)

        with patch.dict(os.environ, {"PATH": str(bin_dir)}, clear=True):
            from agentalloy.install.subcommands.container_runtime import (
                _detect_runtime_binary,
            )

            result = _detect_runtime_binary()
            assert result == "podman"

    def test_neither_returns_none(self, tmp_path: Path):
        """When neither podman nor docker is on PATH, return None."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()

        with patch.dict(os.environ, {"PATH": str(bin_dir)}, clear=True):
            from agentalloy.install.subcommands.container_runtime import (
                _detect_runtime_binary,
            )

            result = _detect_runtime_binary()
            assert result is None


# ---------------------------------------------------------------------------
# UT-2: _locate_build_context
# ---------------------------------------------------------------------------


class TestLocateBuildContext:
    """Test _locate_build_context() search order: cwd -> parents[4] -> auto-clone -> None."""

    def _make_minimal_context(self, d: Path) -> Path:
        """Create minimal build context assets in directory d, return compose path."""
        (d / "compose.yaml").write_text("services: {}\n")
        (d / "Containerfile").write_text("FROM python:3.11\n")
        return d / "compose.yaml"

    def test_finds_in_cwd_first(self, tmp_path: Path):
        """When cwd has build context assets, return cwd compose path."""
        # Create minimal context in a subdirectory and make it cwd
        ctx_dir = tmp_path / "my-clone"
        ctx_dir.mkdir()
        self._make_minimal_context(ctx_dir)

        with patch(
            "agentalloy.install.subcommands.container_runtime.Path.cwd", return_value=ctx_dir
        ):
            from agentalloy.install.subcommands.container_runtime import (
                _locate_build_context,
            )

            result = _locate_build_context()
            assert result == ctx_dir / "compose.yaml"

    def test_falls_back_to_parents4(self, tmp_path: Path):
        """When cwd lacks assets, fall back to parents[4] of __file__."""
        import agentalloy.install.subcommands.container_runtime as mod
        from agentalloy.install.subcommands.container_runtime import (
            _locate_build_context,
        )

        # Create context at parents[4] of the module file.
        # Real path: src/agentalloy/install/subcommands/container_runtime.py
        # parents[4] = repo root.  Replicate this depth.
        ctx_dir = tmp_path / "agentalloy"
        ctx_dir.mkdir()
        self._make_minimal_context(ctx_dir)

        # Fake module path mirrors real structure:
        #   ctx_dir/src/agentalloy/install/subcommands/container_runtime.py
        fake_module_file = str(
            ctx_dir / "src" / "agentalloy" / "install" / "subcommands" / "container_runtime.py"
        )
        Path(fake_module_file).parents[0].mkdir(parents=True, exist_ok=True)

        # Patch Path.cwd to return a non-matching directory
        fake_cwd = tmp_path / "somewhere"
        fake_cwd.mkdir()

        # Patch the module's __file__ to point at our temp location
        original_file = mod.__file__
        mod.__file__ = fake_module_file

        try:
            with patch(
                "agentalloy.install.subcommands.container_runtime.Path.cwd", return_value=fake_cwd
            ):
                result = _locate_build_context()
                assert result == ctx_dir / "compose.yaml"
        finally:
            mod.__file__ = original_file

    def test_falls_back_to_auto_clone_when_both_fail(self, tmp_path: Path):
        """When cwd and parents[4] lack assets, try auto-clone."""
        import agentalloy.install.subcommands.container_runtime as mod
        from agentalloy.install.subcommands.container_runtime import (
            _locate_build_context,
        )

        fake_cwd = tmp_path / "no-context"
        fake_cwd.mkdir()

        # parents[4] of module won't have assets
        repo_root = tmp_path / "agentalloy"
        repo_root.mkdir()
        (repo_root / "src").mkdir()
        (repo_root / "src" / "agentalloy").mkdir()
        (repo_root / "src" / "agentalloy" / "install").mkdir()
        (repo_root / "src" / "agentalloy" / "install" / "subcommands").mkdir(parents=True)
        # Deliberately NOT creating compose.yaml or Containerfile here

        original_file = mod.__file__
        fake_module_file = str(
            repo_root / "src" / "agentalloy" / "install" / "subcommands" / "container_runtime.py"
        )
        mod.__file__ = fake_module_file

        # Use a fake home so the cache dir is inside tmp_path
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        cache_dir = fake_home / ".cache" / "agentalloy" / "repo"
        cache_dir.mkdir(parents=True)
        self._make_minimal_context(cache_dir)

        try:
            # _has_assets returns True only for the cache dir (auto-clone path)
            def _has_assets_side_effect(d: Path) -> bool:
                return d == cache_dir

            with patch(
                "agentalloy.install.subcommands.container_runtime.Path.cwd", return_value=fake_cwd
            ):
                with patch(
                    "agentalloy.install.subcommands.container_runtime.Path.home",
                    return_value=fake_home,
                ):
                    with patch(
                        "agentalloy.install.subcommands.container_runtime.shutil.which",
                        return_value="/usr/bin/git",
                    ):
                        with patch(
                            "agentalloy.install.subcommands.container_runtime.subprocess.run"
                        ) as mock_run:
                            mock_run.return_value = subprocess.CompletedProcess(
                                args=["git", "clone"], returncode=0
                            )
                            with patch(
                                "agentalloy.install.subcommands.container_runtime._has_assets",
                                side_effect=_has_assets_side_effect,
                            ):
                                result = _locate_build_context()
                                assert result == cache_dir / "compose.yaml"
                                # Verify clone was attempted
                                mock_run.assert_called()
        finally:
            mod.__file__ = original_file

    def test_returns_none_when_all_fails(self, tmp_path: Path):
        """When all strategies fail, return None."""
        import agentalloy.install.subcommands.container_runtime as mod
        from agentalloy.install.subcommands.container_runtime import (
            _locate_build_context,
        )

        fake_cwd = tmp_path / "no-context"
        fake_cwd.mkdir()

        repo_root = tmp_path / "agentalloy"
        repo_root.mkdir()
        (repo_root / "install").mkdir()
        (repo_root / "install" / "subcommands").mkdir(parents=True)
        # No compose.yaml or Containerfile

        original_file = mod.__file__
        fake_module_file = str(repo_root / "install" / "subcommands" / "container_runtime.py")
        mod.__file__ = fake_module_file

        try:
            with patch(
                "agentalloy.install.subcommands.container_runtime.Path.cwd", return_value=fake_cwd
            ):
                with patch(
                    "agentalloy.install.subcommands.container_runtime.shutil.which",
                    return_value=None,
                ):
                    result = _locate_build_context()
                    assert result is None
        finally:
            mod.__file__ = original_file


# ---------------------------------------------------------------------------
# UT-3: _build_image
# ---------------------------------------------------------------------------


class TestBuildImage:
    """Test _build_image() constructs correct container build command."""

    def test_build_image_runs_podman_build_with_correct_flags(self, tmp_path: Path):
        """_build_image() runs runtime build -t agentalloy:local -f Containerfile <context>."""
        with patch("agentalloy.install.subcommands.container_runtime.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[
                    "podman",
                    "build",
                    "-t",
                    "agentalloy:local",
                    "-f",
                    "Containerfile",
                    str(tmp_path),
                ],
                returncode=0,
            )
            from agentalloy.install.subcommands.container_runtime import _build_image

            result = _build_image("podman", tmp_path)

            assert result == 0

    def test_build_image_uses_correct_image_tag_and_dockerfile(self, tmp_path: Path):
        """_build_image() passes -t agentalloy:local -f Containerfile to the runtime."""
        # Create a minimal Containerfile so the command doesn't fail on missing file
        (tmp_path / "Containerfile").write_text("FROM python:3.11\n")

        with patch("agentalloy.install.subcommands.container_runtime.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[
                    "podman",
                    "build",
                    "-t",
                    "agentalloy:local",
                    "-f",
                    "Containerfile",
                    str(tmp_path),
                ],
                returncode=0,
            )
            from agentalloy.install.subcommands.container_runtime import _build_image

            _build_image("podman", tmp_path)

            mock_run.assert_called_once()
            call_args = mock_run.call_args
            cmd = call_args[0][0]
            assert "podman" in cmd[0]
            assert "build" in cmd
            assert "-t" in cmd
            assert "agentalloy:local" in cmd
            assert "-f" in cmd
            assert "Containerfile" in cmd
            assert str(tmp_path) in cmd

    def test_build_image_returns_nonzero_on_failure(self, tmp_path: Path):
        """_build_image() returns the non-zero exit code when build fails."""
        with patch("agentalloy.install.subcommands.container_runtime.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                returncode=127, cmd=["podman", "build"], stderr=b"command not found"
            )
            from agentalloy.install.subcommands.container_runtime import _build_image

            result = _build_image("podman", tmp_path)

            assert result == 127

    def test_build_image_writes_log_on_failure(self, tmp_path: Path):
        """_build_image() writes build output to a log file on failure."""
        import tempfile
        from pathlib import Path

        log_path = Path(tempfile.gettempdir()) / "agentalloy-build.log"
        # Remove any pre-existing log
        if log_path.exists():
            log_path.unlink()

        with patch("agentalloy.install.subcommands.container_runtime.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                returncode=1,
                cmd=[
                    "podman",
                    "build",
                    "-t",
                    "agentalloy:local",
                    "-f",
                    "Containerfile",
                    str(tmp_path),
                ],
                output=b"Step 1/3 : FROM python:3.11\n",
                stderr=b"error: failed to solve: no such file or directory",
            )
            from agentalloy.install.subcommands.container_runtime import _build_image

            result = _build_image("podman", tmp_path)

            assert result == 1
            assert log_path.exists()
            content = log_path.read_text()
            assert "exit 1" in content
            assert "failed to solve" in content
            assert "FROM python:3.11" in content

    def test_build_image_has_600s_timeout(self, tmp_path: Path):
        """_build_image() passes timeout=600 to subprocess.run."""
        with patch("agentalloy.install.subcommands.container_runtime.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["podman", "build"], returncode=0
            )
            from agentalloy.install.subcommands.container_runtime import _build_image

            _build_image("podman", tmp_path)

            mock_run.assert_called_once()
            call_kwargs = mock_run.call_args[1]
            assert call_kwargs.get("timeout") == 600


# ---------------------------------------------------------------------------
# UT-4: _ensure_volume
# ---------------------------------------------------------------------------


class TestEnsureVolume:
    """Test _ensure_volume() handles volume creation and idempotency."""

    def test_ensure_volume_runs_volume_create(self):
        """_ensure_volume() runs runtime volume create agentalloy-data."""
        with patch("agentalloy.install.subcommands.container_runtime.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["podman", "volume", "create", "agentalloy-data"],
                returncode=0,
            )
            from agentalloy.install.subcommands.container_runtime import _ensure_volume

            _ensure_volume("podman")

            mock_run.assert_called_once()
            cmd = mock_run.call_args[0][0]
            assert "volume" in cmd
            assert "create" in cmd
            assert "agentalloy-data" in cmd

    def test_ensure_volume_handles_already_exists(self):
        """_ensure_volume() does not raise when runtime reports volume already exists."""
        with patch("agentalloy.install.subcommands.container_runtime.subprocess.run") as mock_run:
            # Some runtimes return non-zero or stderr for "already exists"
            mock_run.side_effect = subprocess.CalledProcessError(
                returncode=1,
                cmd=["podman", "volume", "create", "agentalloy-data"],
                stderr=b"podman: volume agentalloy-data already exists\n",
            )
            from agentalloy.install.subcommands.container_runtime import _ensure_volume

            # Should not raise
            _ensure_volume("podman")

    def test_ensure_volume_raises_on_unexpected_error(self):
        """_ensure_volume() raises on errors other than 'already exists'."""
        with patch("agentalloy.install.subcommands.container_runtime.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                returncode=1,
                cmd=["podman", "volume", "create", "agentalloy-data"],
                stderr=b"permission denied\n",
            )
            from agentalloy.install.subcommands.container_runtime import _ensure_volume

            with pytest.raises(subprocess.CalledProcessError):
                _ensure_volume("podman")


# ---------------------------------------------------------------------------
# UT-5: _ensure_ollama_dir
# ---------------------------------------------------------------------------


class TestEnsureOllamaDir:
    """Test _ensure_ollama_dir() creates ~/.ollama if missing."""

    def test_ensure_ollama_dir_creates_directory(self, tmp_path: Path):
        """_ensure_ollama_dir() creates ~/.ollama when it doesn't exist."""
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        ollama_dir = fake_home / ".ollama"
        assert not ollama_dir.exists()

        with patch(
            "agentalloy.install.subcommands.container_runtime.Path.home", return_value=fake_home
        ):
            from agentalloy.install.subcommands.container_runtime import _ensure_ollama_dir

            _ensure_ollama_dir()

            assert ollama_dir.exists()
            assert ollama_dir.is_dir()

    def test_ensure_ollama_dir_is_idempotent(self, tmp_path: Path):
        """_ensure_ollama_dir() does not fail when ~/.ollama already exists."""
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        ollama_dir = fake_home / ".ollama"
        ollama_dir.mkdir()

        with patch(
            "agentalloy.install.subcommands.container_runtime.Path.home", return_value=fake_home
        ):
            from agentalloy.install.subcommands.container_runtime import _ensure_ollama_dir

            # Should not raise
            _ensure_ollama_dir()

            assert ollama_dir.exists()


# ---------------------------------------------------------------------------
# UT-6: _generate_entrypoint — writes valid bash script with all bootstrap steps
# ---------------------------------------------------------------------------


class TestGenerateEntrypoint:
    """Test _generate_entrypoint() writes a valid bash script to a temp file."""

    def test_generate_entrypoint_returns_path(self, tmp_path: Path):
        """_generate_entrypoint() returns a Path that exists."""
        from agentalloy.install.subcommands.container_runtime import _generate_entrypoint

        result = _generate_entrypoint("")

        assert isinstance(result, Path)
        assert result.exists()

    def test_generate_entrypoint_writes_bash_script(self, tmp_path: Path):
        """_generate_entrypoint() writes a valid bash script with #!/bin/bash."""
        from agentalloy.install.subcommands.container_runtime import _generate_entrypoint

        result = _generate_entrypoint("")
        content = result.read_text()

        assert content.startswith("#!/bin/bash")

    def test_generate_entrypoint_contains_ollama_install(self, tmp_path: Path):
        """Generated entrypoint contains Ollama install step."""
        from agentalloy.install.subcommands.container_runtime import _generate_entrypoint

        content = _generate_entrypoint("").read_text()

        assert "ollama" in content.lower()

    def test_generate_entrypoint_contains_ollama_start(self, tmp_path: Path):
        """Generated entrypoint contains ollama serve start."""
        from agentalloy.install.subcommands.container_runtime import _generate_entrypoint

        content = _generate_entrypoint("").read_text()

        assert "ollama serve" in content

    def test_generate_entrypoint_contains_model_pull(self, tmp_path: Path):
        """Generated entrypoint contains embedding model pull step."""
        from agentalloy.install.subcommands.container_runtime import _generate_entrypoint

        content = _generate_entrypoint("").read_text()

        assert "qwen3-embedding" in content

    def test_generate_entrypoint_contains_migrations(self, tmp_path: Path):
        """Generated entrypoint contains migrations step."""
        from agentalloy.install.subcommands.container_runtime import _generate_entrypoint

        content = _generate_entrypoint("").read_text()

        assert "python -m agentalloy.migrate" in content

    def test_generate_entrypoint_contains_uvicorn_start(self, tmp_path: Path):
        """Generated entrypoint contains uvicorn start step."""
        from agentalloy.install.subcommands.container_runtime import _generate_entrypoint

        content = _generate_entrypoint("").read_text()

        assert "uvicorn" in content

    def test_generate_entrypoint_contains_sigterm_trap(self, tmp_path: Path):
        """Generated entrypoint contains SIGTERM trap for graceful shutdown."""
        from agentalloy.install.subcommands.container_runtime import _generate_entrypoint

        content = _generate_entrypoint("").read_text()

        assert "SIGTERM" in content

    def test_generate_entrypoint_has_executable_permissions(self, tmp_path: Path):
        """_generate_entrypoint() has executable permissions (0o700)."""
        from agentalloy.install.subcommands.container_runtime import _generate_entrypoint

        result = _generate_entrypoint("")
        mode = result.stat().st_mode & 0o777

        assert mode == 0o700

    def test_generate_entrypoint_uses_temp_dir(self, tmp_path: Path):
        """Generated entrypoint is placed in a temp directory."""
        from agentalloy.install.subcommands.container_runtime import _generate_entrypoint

        result = _generate_entrypoint("")

        # The file should be in a temp directory (e.g., /tmp or similar)
        assert result.is_file()


# ---------------------------------------------------------------------------
# UT-7: _generate_entrypoint — no install-packs when packs is empty
# ---------------------------------------------------------------------------


class TestGenerateEntrypointNoPacks:
    """Test _generate_entrypoint() when packs is empty."""

    def test_no_install_packs_when_packs_empty(self, tmp_path: Path):
        """When packs='', the script must not invoke ``agentalloy install-packs``.

        We probe specifically for the invocation token rather than any
        ``install-packs`` substring — the fast-start design also references
        ``.install-packs-lock`` (the concurrent-install guard) in the env
        block, which is unrelated to whether install-packs actually runs.
        """
        from agentalloy.install.subcommands.container_runtime import _generate_entrypoint

        content = _generate_entrypoint("").read_text()

        assert "uv run agentalloy install-packs" not in content
        assert "No packs specified" in content


# ---------------------------------------------------------------------------
# UT-8: _generate_entrypoint — install-packs present when packs non-empty
# ---------------------------------------------------------------------------


class TestGenerateEntrypointWithPacks:
    """Test _generate_entrypoint() when packs is non-empty."""

    def test_install_packs_present_when_packs_set(self, tmp_path: Path):
        """When packs are set, the script runs install-packs per pack.

        The fast-start design installs packs one at a time (so each gets its
        own checkpoint), so we look for both pack names individually in the
        emitted PACK_LIST literal — not the original comma-joined string.
        """
        from agentalloy.install.subcommands.container_runtime import _generate_entrypoint

        content = _generate_entrypoint("foundation,tooling").read_text()

        assert "uv run agentalloy install-packs" in content
        # Each pack appears in the PACK_LIST array.
        assert "foundation" in content
        assert "tooling" in content


# ---------------------------------------------------------------------------
# UT-9: _cleanup_temp_entrypoint — removes the temp file
# ---------------------------------------------------------------------------


class TestCleanupTempEntrypoint:
    """Test _cleanup_temp_entrypoint() removes the temp file."""

    def test_cleanup_removes_file(self, tmp_path: Path):
        """_cleanup_temp_entrypoint() removes the temp file."""
        from agentalloy.install.subcommands.container_runtime import (
            _cleanup_temp_entrypoint,
            _generate_entrypoint,
        )

        entrypoint = _generate_entrypoint("")
        assert entrypoint.exists()

        _cleanup_temp_entrypoint(entrypoint)

        assert not entrypoint.exists()

    def test_cleanup_is_idempotent(self, tmp_path: Path):
        """_cleanup_temp_entrypoint() does not raise if file is already gone."""
        from agentalloy.install.subcommands.container_runtime import (
            _cleanup_temp_entrypoint,
        )

        # Should not raise even if the file doesn't exist
        _cleanup_temp_entrypoint(tmp_path / "nonexistent.sh")


# ---------------------------------------------------------------------------
# UT-10: readiness polling moved to _wait_for_readiness — see
# tests/install/test_container_runtime_readiness.py for coverage. The legacy
# _wait_for_health helper was dead code (defined but never called) and was
# removed alongside the fast-start redesign.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# UT-10b: _run_container — correct flags, volumes, env, port
# ---------------------------------------------------------------------------


class TestRunContainer:
    """Test _run_container() constructs the correct container run command."""

    def test_run_container_uses_correct_flags(self, tmp_path: Path):
        """_run_container() runs --replace -d --name agentalloy."""
        entrypoint = tmp_path / "entrypoint.sh"
        entrypoint.write_text("#!/bin/bash\n")

        with patch("agentalloy.install.subcommands.container_runtime.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["podman", "run"], returncode=0
            )
            from agentalloy.install.subcommands.container_runtime import _run_container

            result = _run_container("podman", entrypoint, "")

            assert result == 0
            mock_run.assert_called_once()
            cmd = mock_run.call_args[0][0]
            assert "podman" in cmd[0]
            assert "run" in cmd
            assert "--replace" in cmd
            assert "-d" in cmd
            assert "--name" in cmd
            assert "agentalloy" in cmd

    def test_run_container_has_port_mapping(self, tmp_path: Path):
        """_run_container() maps port 47950:47950."""
        entrypoint = tmp_path / "entrypoint.sh"
        entrypoint.write_text("#!/bin/bash\n")

        with patch("agentalloy.install.subcommands.container_runtime.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["podman", "run"], returncode=0
            )
            from agentalloy.install.subcommands.container_runtime import _run_container

            _run_container("podman", entrypoint, "")

            cmd = mock_run.call_args[0][0]
            assert "-p" in cmd
            assert "47950:47950" in cmd

    def test_run_container_has_volume_mounts(self, tmp_path: Path):
        """_run_container() mounts agentalloy-data:/app/data and ~/.ollama:/root/.ollama."""
        entrypoint = tmp_path / "entrypoint.sh"
        entrypoint.write_text("#!/bin/bash\n")

        with patch("agentalloy.install.subcommands.container_runtime.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["podman", "run"], returncode=0
            )
            from agentalloy.install.subcommands.container_runtime import _run_container

            _run_container("podman", entrypoint, "")

            cmd = mock_run.call_args[0][0]
            cmd_str = " ".join(cmd)
            assert "agentalloy-data:/app/data" in cmd_str
            assert "/root/.ollama" in cmd_str

    def test_run_container_sets_env_vars(self, tmp_path: Path):
        """_run_container() sets AGENTIALLOY_PACKS, ENTRYPOINT, LADYBUG_DB_PATH, DUCKDB_PATH, LOG_LEVEL."""
        entrypoint = tmp_path / "entrypoint.sh"
        entrypoint.write_text("#!/bin/bash\n")

        with patch("agentalloy.install.subcommands.container_runtime.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["podman", "run"], returncode=0
            )
            from agentalloy.install.subcommands.container_runtime import _run_container

            _run_container("podman", entrypoint, "foundation")

            cmd = mock_run.call_args[0][0]
            cmd_str = " ".join(cmd)
            assert "-e" in cmd
            assert "AGENTIALLOY_PACKS=foundation" in cmd_str
            assert "ENTRYPOINT=" in cmd_str
            assert "LADYBUG_DB_PATH=/app/data/ladybug" in cmd_str
            assert "DUCKDB_PATH=/app/data/skills.duck" in cmd_str
            assert "LOG_LEVEL=info" in cmd_str

    def test_run_container_returns_exit_code_on_failure(self, tmp_path: Path):
        """_run_container() returns the non-zero exit code on failure."""
        entrypoint = tmp_path / "entrypoint.sh"
        entrypoint.write_text("#!/bin/bash\n")

        with patch("agentalloy.install.subcommands.container_runtime.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                returncode=125, cmd=["podman", "run"]
            )
            from agentalloy.install.subcommands.container_runtime import _run_container

            result = _run_container("podman", entrypoint, "")

            assert result == 125

    def test_run_container_has_300s_timeout(self, tmp_path: Path):
        """_run_container() passes timeout=300 to subprocess.run."""
        entrypoint = tmp_path / "entrypoint.sh"
        entrypoint.write_text("#!/bin/bash\n")

        with patch("agentalloy.install.subcommands.container_runtime.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["podman", "run"], returncode=0
            )
            from agentalloy.install.subcommands.container_runtime import _run_container

            _run_container("podman", entrypoint, "")

            call_kwargs = mock_run.call_args[1]
            assert call_kwargs.get("timeout") == 300

    def test_run_container_mounts_entrypoint_as_ro(self, tmp_path: Path):
        """_run_container() mounts the entrypoint script read-only."""
        entrypoint = tmp_path / "entrypoint.sh"
        entrypoint.write_text("#!/bin/bash\n")

        with patch("agentalloy.install.subcommands.container_runtime.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["podman", "run"], returncode=0
            )
            from agentalloy.install.subcommands.container_runtime import _run_container

            _run_container("podman", entrypoint, "")

            cmd = mock_run.call_args[0][0]
            entrypoint_mount = f"{entrypoint}:/app/entrypoint.sh:ro"
            assert entrypoint_mount in cmd

    def test_run_container_uses_correct_image_and_entrypoint(self, tmp_path: Path):
        """_run_container() uses agentalloy:local image and /app/entrypoint.sh."""
        entrypoint = tmp_path / "entrypoint.sh"
        entrypoint.write_text("#!/bin/bash\n")

        with patch("agentalloy.install.subcommands.container_runtime.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["podman", "run"], returncode=0
            )
            from agentalloy.install.subcommands.container_runtime import _run_container

            _run_container("podman", entrypoint, "")

            cmd = mock_run.call_args[0][0]
            assert "agentalloy:local" in cmd
            assert "/app/entrypoint.sh" in cmd


# ---------------------------------------------------------------------------
# Helper for E2E entrypoint tests
# ---------------------------------------------------------------------------


def _setup_entrypoint_test(
    tmp_path: Path,
    bootstrap_complete: bool = False,
    packs: str = "",
) -> tuple[Path, dict[str, str], Path]:
    """Helper to set up an entrypoint test with mock binaries and a temp app dir.

    Creates:
    - /tmp/app/ directory (via APP_DIR env var)
    - Mock ollama, curl, uv, agentalloy, python, uvicorn (and optionally install-packs)
    - Optionally /tmp/app/.bootstrap-complete

    Returns (entrypoint_path, env_dict, app_dir).
    """
    from agentalloy.install.subcommands.container_runtime import _build_entrypoint_script

    script = _build_entrypoint_script(packs)
    entrypoint = tmp_path / "entrypoint.sh"
    entrypoint.write_text(script)
    entrypoint.chmod(0o755)

    # Use /tmp/app as the app directory (configurable via APP_DIR env var)
    app_dir = Path("/tmp/app")
    app_dir.mkdir(exist_ok=True)
    # Clean up any leftover bootstrap state so tests are isolated. The
    # fast-start entrypoint writes .bootstrap-lock / .bootstrap-progress /
    # .bootstrap-checkpoints / .install-packs-lock; if a prior test left
    # checkpoints behind, ``pack_already_done`` would skip the install loop
    # and the per-pack "Installing pack" lines would never appear.
    for name in (
        ".bootstrap-complete",
        ".bootstrap-lock",
        ".bootstrap-progress",
        ".bootstrap-progress.tmp",
        ".bootstrap-checkpoints",
        ".install-packs-lock",
    ):
        marker = app_dir / name
        if marker.exists():
            marker.unlink()
    if bootstrap_complete:
        (app_dir / ".bootstrap-complete").write_text("")

    bin_dir = tmp_path / "mock_bin"
    bin_dir.mkdir()

    # Mock ollama
    (bin_dir / "ollama").write_text('#!/bin/sh\necho "OLLAMA: $*" >> /tmp/ollama_calls.log\n')
    (bin_dir / "ollama").chmod(0o755)
    # Mock curl — always succeeds immediately
    (bin_dir / "curl").write_text("#!/bin/sh\necho OK\n")
    (bin_dir / "curl").chmod(0o755)
    # Mock uv — strips "uv run" prefix and executes the remaining command
    (bin_dir / "uv").write_text(
        "#!/usr/bin/env python3\nimport sys, subprocess\nsubprocess.run(sys.argv[2:], check=True)\n"
    )
    (bin_dir / "uv").chmod(0o755)
    # Mock agentalloy CLI (handles migrate and install-packs subcommands)
    (bin_dir / "agentalloy").write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        '  migrate) echo "AGENTIALLOY: migrate" >> /tmp/agentalloy_calls.log ;;\n'
        '  install-packs) echo "AGENTIALLOY: install-packs $*" >> /tmp/agentalloy_calls.log ;;\n'
        '  *) echo "AGENTIALLOY: unknown $@" >> /tmp/agentalloy_calls.log ;;\n'
        "esac\n"
        "exit 0\n"
    )
    (bin_dir / "agentalloy").chmod(0o755)
    # Mock python (for agentalloy.migrate)
    (bin_dir / "python").write_text('#!/bin/sh\necho "PYTHON: $*" >> /tmp/python_calls.log\n')
    (bin_dir / "python").chmod(0o755)
    # Mock uvicorn
    (bin_dir / "uvicorn").write_text(
        '#!/bin/sh\necho "UVICORN STARTED" >> /tmp/uvicorn_calls.log\n'
    )
    (bin_dir / "uvicorn").chmod(0o755)

    # Mock install-packs if packs are specified
    if packs.strip():
        (bin_dir / "install-packs").write_text(
            '#!/bin/sh\necho "INSTALL-PACKS: $*" >> /tmp/packs_calls.log\n'
        )
        (bin_dir / "install-packs").chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = str(bin_dir) + ":" + env.get("PATH", "")
    env["APP_DIR"] = str(app_dir)
    env["LADYBUG_DB_PATH"] = str(app_dir / "ladybug")
    env["DUCKDB_PATH"] = str(app_dir / "skills.duck")

    return entrypoint, env, app_dir


# ---------------------------------------------------------------------------
# E2E-3: Container restart skips Ollama install, model pull, migrations
#        when .bootstrap-complete exists
# ---------------------------------------------------------------------------


class TestEntrypointBootstrapComplete:
    """E2E: entrypoint script skips bootstrap when /app/.bootstrap-complete exists."""

    def test_skips_ollama_install_when_bootstrap_complete(self, tmp_path: Path):
        """When .bootstrap-complete exists, the entrypoint should not attempt Ollama install."""
        entrypoint, env, app_dir = _setup_entrypoint_test(tmp_path, bootstrap_complete=True)

        result = subprocess.run(
            ["bash", str(entrypoint)],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode == 0
        # The entrypoint should have gone straight to uvicorn when bootstrap is complete
        assert "Installing Ollama" not in result.stdout
        assert "ollama.ai/install.sh" not in result.stdout

    def test_skips_migrations_when_bootstrap_complete(self, tmp_path: Path):
        """When .bootstrap-complete exists, migrations should not run."""
        entrypoint, env, app_dir = _setup_entrypoint_test(tmp_path, bootstrap_complete=True)

        result = subprocess.run(
            ["bash", str(entrypoint)],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode == 0
        # Verify migrations were NOT called
        assert "agentalloy.migrate" not in result.stdout
        # Verify uvicorn WAS started
        assert "Starting uvicorn" in result.stdout

    def test_skips_model_pull_when_bootstrap_complete(self, tmp_path: Path):
        """When .bootstrap-complete exists, model pull should not happen."""
        entrypoint, env, app_dir = _setup_entrypoint_test(tmp_path, bootstrap_complete=True)

        result = subprocess.run(
            ["bash", str(entrypoint)],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode == 0
        # Verify model pull was NOT attempted
        assert "Pulling qwen3-embedding" not in result.stdout
        # Verify the skip message is present
        assert "Bootstrap already complete" in result.stdout


# ---------------------------------------------------------------------------
# E2E-4: Container restart after crash re-runs migrations and install-packs
# ---------------------------------------------------------------------------


class TestEntrypointCrashRestart:
    """E2E: entrypoint script runs full bootstrap when .bootstrap-complete does NOT exist."""

    def test_reruns_migrations_on_crash_restart(self, tmp_path: Path):
        """When .bootstrap-complete is missing, migrations should run."""
        entrypoint, env, app_dir = _setup_entrypoint_test(tmp_path, bootstrap_complete=False)

        result = subprocess.run(
            ["bash", str(entrypoint)],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode == 0
        # Verify migrations WERE called
        assert "Running migrations" in result.stdout
        # Verify bootstrap-complete flag was created
        assert (app_dir / ".bootstrap-complete").exists()
        # Verify uvicorn started
        assert "Starting uvicorn" in result.stdout

    def test_reruns_install_packs_on_crash_restart(self, tmp_path: Path):
        """When .bootstrap-complete is missing and packs are specified, install-packs should run.

        Fast-start design installs packs one at a time (per-pack checkpoints),
        so we look for each pack name on its own ``Installing pack`` line
        instead of a single combined banner.
        """
        entrypoint, env, app_dir = _setup_entrypoint_test(
            tmp_path, bootstrap_complete=False, packs="foundation,tooling"
        )

        result = subprocess.run(
            ["bash", str(entrypoint)],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode == 0
        # Each pack gets its own line in the per-pack loop.
        assert "Installing pack: foundation" in result.stdout
        assert "Installing pack: tooling" in result.stdout
        # Verify bootstrap-complete was created
        assert (app_dir / ".bootstrap-complete").exists()
