"""Proxy context — working directory resolution and phase reading tests."""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

from agentalloy.api.proxy_context import (
    decode_proj_token,
    encode_proj_token,
    read_phase,
    resolve_working_dir,
)
from agentalloy.api.proxy_models import ProxyMessage, ProxyRequest

_MSG = [ProxyMessage(role="user", content="hello")]


class TestResolveWorkingDir:
    """Test resolve_working_dir() resolution order."""

    def test_metadata_cwd_has_priority(self, tmp_path: Path) -> None:
        """metadata.cwd takes highest priority."""
        req = ProxyRequest(
            model="gpt-4",
            messages=_MSG,
            metadata={"cwd": str(tmp_path)},
        )
        result = resolve_working_dir(req)
        assert result == tmp_path

    def test_env_var_fallback(self) -> None:
        """AGENTALLOY_PROJECT_DIR env var used when no metadata."""
        req = ProxyRequest(
            model="gpt-4",
            messages=_MSG,
        )
        with mock.patch.dict(os.environ, {"AGENTALLOY_PROJECT_DIR": "/tmp/project"}):
            result = resolve_working_dir(req)
        assert result == Path("/tmp/project")

    def test_process_cwd_last_resort(self) -> None:
        """Path.cwd() used as last fallback."""
        req = ProxyRequest(
            model="gpt-4",
            messages=_MSG,
        )
        # Unset AGENTALLOY_PROJECT_DIR
        env = os.environ.copy()
        env.pop("AGENTALLOY_PROJECT_DIR", None)
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch("pathlib.Path.cwd", return_value=Path("/proc/cwd")),
        ):
            result = resolve_working_dir(req)
        assert result == Path("/proc/cwd")

    def test_metadata_cwd_beats_env_var(self, tmp_path: Path) -> None:
        """metadata.cwd takes priority over AGENTALLOY_PROJECT_DIR env var."""
        req = ProxyRequest(
            model="gpt-4",
            messages=_MSG,
            metadata={"cwd": str(tmp_path)},
        )
        with mock.patch.dict(os.environ, {"AGENTALLOY_PROJECT_DIR": "/env/project"}):
            result = resolve_working_dir(req)
        assert result == tmp_path

    def test_metadata_none_uses_env(self) -> None:
        """metadata=None falls through to env var."""
        req = ProxyRequest(
            model="gpt-4",
            messages=_MSG,
            metadata=None,
        )
        with mock.patch.dict(os.environ, {"AGENTALLOY_PROJECT_DIR": "/env/project"}):
            result = resolve_working_dir(req)
        assert result == Path("/env/project")

    def test_metadata_without_cwd_key(self) -> None:
        """metadata exists but has no 'cwd' key — falls through."""
        req = ProxyRequest(
            model="gpt-4",
            messages=_MSG,
            metadata={"other": "value"},
        )
        with mock.patch.dict(os.environ, {"AGENTALLOY_PROJECT_DIR": "/env/project"}):
            result = resolve_working_dir(req)
        assert result == Path("/env/project")

    def test_project_dir_override_wins(self, tmp_path: Path) -> None:
        """TC6: project_dir_override (the decoded /proj token) beats metadata.cwd."""
        req = ProxyRequest(
            model="gpt-4",
            messages=_MSG,
            metadata={"cwd": "/some/other/dir"},
        )
        with mock.patch.dict(os.environ, {"AGENTALLOY_PROJECT_DIR": "/env/project"}):
            result = resolve_working_dir(req, project_dir_override=tmp_path)
        assert result == tmp_path

    def test_override_none_falls_through(self, tmp_path: Path) -> None:
        """An explicit None override is equivalent to not passing one."""
        req = ProxyRequest(model="gpt-4", messages=_MSG, metadata={"cwd": str(tmp_path)})
        assert resolve_working_dir(req, project_dir_override=None) == tmp_path


class TestProjToken:
    """TC4 — /proj/<token> discriminator codec round-trips and rejects junk."""

    def test_round_trip_plain(self, tmp_path: Path) -> None:
        token = encode_proj_token(tmp_path)
        assert decode_proj_token(token) == Path(os.path.realpath(tmp_path))

    def test_round_trip_spaces_and_unicode(self, tmp_path: Path) -> None:
        weird = tmp_path / "a repo — δοκιμή"
        weird.mkdir()
        token = encode_proj_token(weird)
        # URL-safe alphabet only (no +, /, or padding) so it's a clean path segment.
        assert all(c.isalnum() or c in "-_" for c in token)
        assert decode_proj_token(token) == Path(os.path.realpath(weird))

    def test_trailing_slash_normalized(self, tmp_path: Path) -> None:
        with_slash = encode_proj_token(f"{tmp_path}/")
        without = encode_proj_token(str(tmp_path))
        assert with_slash == without  # realpath strips the trailing slash

    def test_symlink_resolved(self, tmp_path: Path) -> None:
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        assert decode_proj_token(encode_proj_token(link)) == Path(os.path.realpath(real))

    def test_rejects_malformed_token(self) -> None:
        import pytest

        for junk in ("not!base64!", "", "Zm9v"):  # last decodes to "foo" (relative → reject)
            with pytest.raises(ValueError):
                decode_proj_token(junk)


class TestReadPhase:
    """Test read_phase() file reading."""

    def test_existing_phase_file(self, tmp_path: Path) -> None:
        """Returns stripped content of .agentalloy/phase."""
        phase_dir = tmp_path / ".agentalloy"
        phase_dir.mkdir()
        (phase_dir / "phase").write_text("  build  \n")
        result = read_phase(tmp_path)
        assert result == "build"

    def test_missing_phase_file(self, tmp_path: Path) -> None:
        """Returns None when .agentalloy/phase does not exist."""
        result = read_phase(tmp_path)
        assert result is None

    def test_missing_agentalloy_dir(self, tmp_path: Path) -> None:
        """Returns None when .agentalloy directory does not exist."""
        result = read_phase(tmp_path)
        assert result is None

    def test_empty_phase_file(self, tmp_path: Path) -> None:
        """Returns None when phase file is empty."""
        phase_dir = tmp_path / ".agentalloy"
        phase_dir.mkdir()
        (phase_dir / "phase").write_text("")
        result = read_phase(tmp_path)
        assert result is None

    def test_whitespace_only_phase_file(self, tmp_path: Path) -> None:
        """Returns None when phase file is only whitespace."""
        phase_dir = tmp_path / ".agentalloy"
        phase_dir.mkdir()
        (phase_dir / "phase").write_text("   \n  \n  ")
        result = read_phase(tmp_path)
        assert result is None

    def test_read_error_returns_none(self, tmp_path: Path) -> None:
        """Returns None on OS-level read errors."""
        phase_dir = tmp_path / ".agentalloy"
        phase_dir.mkdir()
        phase_file = phase_dir / "phase"
        phase_file.write_text("build")
        with mock.patch("pathlib.Path.read_text", side_effect=OSError("permission")):
            result = read_phase(tmp_path)
        assert result is None
