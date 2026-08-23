"""Proxy context — working directory resolution and phase reading tests."""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest

from agentalloy.api.proxy_context import (
    decode_proj_token,
    encode_proj_token,
    read_phase,
    resolve_working_dir,
)
from agentalloy.api.proxy_models import ProxyMessage, ProxyRequest
from tests.support import seed_phase

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

    def test_project_dir_override_wins_when_metadata_absent(self, tmp_path: Path) -> None:
        """TC6: with no metadata.cwd, the decoded /proj token still wins."""
        req = ProxyRequest(model="gpt-4", messages=_MSG, metadata=None)
        with mock.patch.dict(os.environ, {"AGENTALLOY_PROJECT_DIR": "/env/project"}):
            result = resolve_working_dir(req, project_dir_override=tmp_path)
        assert result == tmp_path

    def test_project_dir_override_and_metadata_agree(self, tmp_path: Path) -> None:
        """Token and metadata.cwd naming the same realpath resolve to that repo."""
        req = ProxyRequest(
            model="gpt-4",
            messages=_MSG,
            metadata={"cwd": str(tmp_path)},
        )
        with mock.patch.dict(os.environ, {"AGENTALLOY_PROJECT_DIR": "/env/project"}):
            result = resolve_working_dir(req, project_dir_override=tmp_path)
        assert result == tmp_path

    def test_metadata_cwd_wins_over_disagreeing_token(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A stale token that disagrees with metadata.cwd loses to the per-request signal.

        The token is captured once at session start; a session that started in the
        wrong directory (or predates a re-wire) carries a stale token. The
        per-request metadata.cwd must override it and the mismatch must be
        surfaced (anomalies B1/B4/B5).
        """
        req = ProxyRequest(
            model="gpt-4",
            messages=_MSG,
            metadata={"cwd": "/some/other/dir"},
        )
        with (
            mock.patch.dict(os.environ, {"AGENTALLOY_PROJECT_DIR": "/env/project"}),
            caplog.at_level("WARNING", logger="agentalloy.api.proxy_context"),
        ):
            result = resolve_working_dir(req, project_dir_override=tmp_path)
        assert result == Path("/some/other/dir")
        assert any("/proj token" in rec.message for rec in caplog.records)

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
        for junk in ("not!base64!", "", "Zm9v"):  # last decodes to "foo" (relative → reject)
            with pytest.raises(ValueError):
                decode_proj_token(junk)


class TestReadPhase:
    """``read_phase`` reads the SDD state store, never a file."""

    def test_existing_phase(self, tmp_path: Path) -> None:
        seed_phase(tmp_path, "build")
        assert read_phase(tmp_path) == "build"

    def test_no_phase_recorded(self, tmp_path: Path) -> None:
        assert read_phase(tmp_path) is None

    def test_a_phase_file_is_not_a_source(self, tmp_path: Path) -> None:
        """A leftover file from before the migration must not resurrect a phase."""
        phase_dir = tmp_path / ".agentalloy"
        phase_dir.mkdir()
        (phase_dir / "phase").write_text("build\n")
        assert read_phase(tmp_path) is None

    def test_store_read_error_returns_none(self, tmp_path: Path) -> None:
        """A failing store read is logged and answered None, not raised."""
        seed_phase(tmp_path, "build")
        with mock.patch(
            "agentalloy.storage.state_store.DuckDBStateStore.read_phase",
            side_effect=RuntimeError("db gone"),
        ):
            assert read_phase(tmp_path) is None
