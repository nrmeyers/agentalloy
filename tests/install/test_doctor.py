"""Unit tests for the ``doctor`` subcommand."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from agentalloy.install import state as install_state
from agentalloy.install.subcommands.doctor import (
    _check_duckdb_version,  # pyright: ignore[reportPrivateUsage]
    _check_fts_status,  # pyright: ignore[reportPrivateUsage]
    _check_runner_processes,  # pyright: ignore[reportPrivateUsage]
    _check_service_reachable,  # pyright: ignore[reportPrivateUsage]
    _check_state_consistent,  # pyright: ignore[reportPrivateUsage]
    run_doctor,
)


@pytest.fixture()
def repo_root(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text("")
    return tmp_path


def _minimal_state(root: Path) -> dict[str, Any]:
    """Set up a minimal install state with .env for doctor to read."""
    st = install_state.load_state(root)
    st["completed_steps"] = [{"step": "detect", "completed_at": "2026-01-01"}]
    st["port"] = 8000
    install_state.save_state(st, root)
    # Write a minimal .env
    (root / ".env").write_text(
        "RUNTIME_EMBED_BASE_URL=http://localhost:11434\n"
        "RUNTIME_EMBEDDING_MODEL=qwen3-embedding:0.6b\n"
        "DUCKDB_PATH=./data/skills.duck\n"
        "LADYBUG_DB_PATH=./data/ladybug\n"
    )
    return st


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


class TestServiceReachable:
    def test_service_up(self) -> None:
        body = json.dumps({"status": "ok"}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = body
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("agentalloy.install.subcommands.doctor.urlopen", return_value=mock_resp):
            result = _check_service_reachable(8000)
        assert result["passed"] is True

    def test_service_down(self) -> None:
        from urllib.error import URLError

        with patch(
            "agentalloy.install.subcommands.doctor.urlopen", side_effect=URLError("refused")
        ):
            result = _check_service_reachable(8000)
        assert result["passed"] is False


class TestStateConsistent:
    def test_empty_state_fails(self) -> None:
        result = _check_state_consistent({"completed_steps": []})
        assert result["passed"] is False

    def test_populated_state_passes(self) -> None:
        result = _check_state_consistent(
            {
                "completed_steps": [{"step": "detect"}],
                "harness_files_written": [],
            }
        )
        assert result["passed"] is True


class TestRunnerProcesses:
    def test_no_runners(self) -> None:
        result = _check_runner_processes({"models_pulled": []})
        assert result["passed"] is True

    def test_missing_runner(self) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/ollama"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=1)
            result = _check_runner_processes({"models_pulled": ["ollama:qwen3-embedding:0.6b"]})
        assert result["passed"] is False


class TestFtsStatus:
    def test_duckdb_not_present(self, tmp_path: Path) -> None:
        duck_path = str(tmp_path / "nonexistent.duck")
        result = _check_fts_status(duck_path)
        assert result["passed"] is True
        assert "deferred" in result["detail"].lower()

    def test_fts_index_present(self, tmp_path: Path) -> None:
        import duckdb

        db_path = str(tmp_path / "skills.duck")
        con = duckdb.connect(db_path)
        con.execute("INSTALL fts;")
        con.execute("LOAD fts;")
        con.execute("CREATE TABLE fragment_embeddings (id INT, prose TEXT)")
        con.execute("INSERT INTO fragment_embeddings VALUES (1, 'hello world')")
        con.execute("PRAGMA create_fts_index('fragment_embeddings', 'id', 'prose')")
        con.close()

        result = _check_fts_status(db_path)
        # FTS index exists if fts_main_fragment_embeddings schema has tables
        assert result["passed"] is True
        assert "FTS" in result["detail"]

    def test_fts_index_missing(self, tmp_path: Path) -> None:
        import duckdb

        db_path = str(tmp_path / "skills.duck")
        con = duckdb.connect(db_path)
        con.execute("CREATE TABLE fragment_embeddings (id INT, prose TEXT)")
        con.close()

        result = _check_fts_status(db_path)
        assert result["passed"] is False
        assert "DuckDB 1.5.3" in result["error"]
        assert "NOT an agentalloy issue" in result["error"]


class TestDuckdbVersion:
    def test_version_ok(self) -> None:
        import duckdb

        result = _check_duckdb_version()
        # Current installed version should be >= 1.5.3
        assert result["passed"] is True
        assert "1.5.3" in result["detail"]

    def test_version_parse(self) -> None:
        """Verify version parsing logic handles edge cases."""
        # Test parsing of "1.5.3"
        version_str = "1.5.3"
        parts = version_str.split(".")
        major, minor, patch = int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0
        assert (major, minor, patch) == (1, 5, 3)

        # Test parsing of "1.5"
        version_str = "1.5"
        parts = version_str.split(".")
        major, minor, patch = int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0
        assert (major, minor, patch) == (1, 5, 0)


# ---------------------------------------------------------------------------
# Full doctor
# ---------------------------------------------------------------------------


class TestRunDoctor:
    def test_returns_all_checks(self, repo_root: Path) -> None:
        _minimal_state(repo_root)
        # Mock network calls to avoid real connections
        from urllib.error import URLError

        with (
            patch(
                "agentalloy.install.subcommands.verify.urlopen", side_effect=URLError("no network")
            ),
            patch(
                "agentalloy.install.subcommands.doctor.urlopen", side_effect=URLError("no network")
            ),
        ):
            result = run_doctor(root=repo_root)
        assert result["schema_version"] == 1
        # 6 preflight-early + 8 verify + 6 doctor = 20 (count may grow as
        # checks are added; assert the named ones are all present rather
        # than a brittle total).
        names = [c["name"] for c in result["checks"]]
        assert "agentalloy_service_reachable" in names
        assert "compose_endpoint_works" in names
        assert "state_file_consistent" in names
        assert "runner_processes_present" in names
        assert "fts_index_status" in names
        assert "duckdb_version_ok" in names
        # Preflight early checks now flow through doctor too.
        assert "uv_present" in names
        assert "cli_on_path" in names

    def test_output_shape(self, repo_root: Path) -> None:
        _minimal_state(repo_root)
        from urllib.error import URLError

        with (
            patch(
                "agentalloy.install.subcommands.verify.urlopen", side_effect=URLError("no network")
            ),
            patch(
                "agentalloy.install.subcommands.doctor.urlopen", side_effect=URLError("no network")
            ),
        ):
            result = run_doctor(root=repo_root)
        assert "all_checks_passed" in result
        for check in result["checks"]:
            assert "name" in check
            assert "passed" in check
