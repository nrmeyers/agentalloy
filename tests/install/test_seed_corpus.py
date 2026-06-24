"""Unit tests for the ``seed-corpus`` subcommand.

Maps to test-plan.md § Seed corpus integrity.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agentalloy.install.subcommands.seed_corpus import (
    SCHEMA_VERSION,
    check_corpus,
    corpus_skill_count,
    run,
)


@pytest.fixture()
def repo_root(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text("")
    return tmp_path


@pytest.fixture()
def no_bundled_corpus(monkeypatch: pytest.MonkeyPatch) -> None:
    """Block first-run seed-from-wheel so missing-file tests stay missing.

    Without this, `check_corpus` would auto-copy the real bundled corpus
    out of the wheel into the test's XDG data dir on every call.
    """
    from agentalloy.install import state as install_state

    monkeypatch.setattr(install_state, "bundled_corpus_dir", lambda: None)


@pytest.fixture()
def user_corpus(tmp_path: Path) -> Path:
    """Path to the (test-isolated) user corpus dir created on demand."""
    from agentalloy.install import state as install_state

    p = install_state.corpus_dir()
    p.mkdir(parents=True, exist_ok=True)
    return p


class TestMissingFiles:
    """In the pack-based distribution, missing corpus files trigger the
    `initialized_empty` action — empty stores get materialized so the
    subsequent `install-packs` step can populate them. The old
    `missing_files` action is gone for the fully-missing case."""

    def test_missing_both_initializes_empty(self, repo_root: Path, no_bundled_corpus: None) -> None:
        """Fresh install: neither file exists → init empty stores."""
        result = check_corpus(repo_root)
        assert result["action"] == "initialized_empty"
        assert result["skill_count"] == 0
        assert result["fragment_count"] == 0
        assert "install-packs" in result.get("remediation", "")

    def test_partial_corruption_returns_init_failed(
        self, repo_root: Path, user_corpus: Path, no_bundled_corpus: None
    ) -> None:
        """A pre-existing-but-malformed ladybug path can't be initialized.
        Surface the failure rather than silently overwriting."""
        # Pre-create an empty `ladybug` directory (Kuzu expects a file).
        (user_corpus / "ladybug").mkdir()
        result = check_corpus(repo_root)
        assert result["action"] == "init_failed"
        assert "Could not initialize" in result.get("error", "")


class TestVerifiedPresent:
    @patch("agentalloy.install.subcommands.seed_corpus._check_duckdb")
    def test_verified_when_above_minimum(
        self, mock_duck: MagicMock, repo_root: Path, user_corpus: Path
    ) -> None:
        (user_corpus / "skills.duck").write_bytes(b"fake")
        (user_corpus / "ladybug").mkdir()
        mock_duck.return_value = {
            "skill_count": 93,
            "fragment_count": 1003,
            "embedding_model": "nomic-embed-text-v1.5",
            "embedding_dim": 768,
        }
        result = check_corpus(repo_root)
        assert result["action"] == "verified_present"
        assert result["skill_count"] == 93
        assert result["fragment_count"] == 1003
        assert result["embedding_model"] == "nomic-embed-text-v1.5"
        assert result["embedding_dim"] == 768
        assert result["schema_version"] == SCHEMA_VERSION


class TestUnderMinimumSkillCount:
    @patch("agentalloy.install.subcommands.seed_corpus._check_duckdb")
    def test_under_minimum_still_flagged(
        self, mock_duck: MagicMock, repo_root: Path, user_corpus: Path
    ) -> None:
        """A populated-but-under-minimum corpus still triggers the
        ``missing_files`` action — that path detects integrity problems
        in a corpus that DOES have the files but is suspiciously small.
        Distinct from the `initialized_empty` path (no files at all)."""
        (user_corpus / "skills.duck").write_bytes(b"fake")
        (user_corpus / "ladybug").mkdir()
        mock_duck.return_value = {
            "skill_count": 10,
            "fragment_count": 50,
            "embedding_model": "nomic-embed-text-v1.5",
            "embedding_dim": 768,
        }
        result = check_corpus(repo_root)
        assert result["action"] == "missing_files"
        assert result["skill_count"] == 10

    @patch("agentalloy.install.subcommands.seed_corpus._check_duckdb")
    def test_remediation_includes_migrate_step(
        self, mock_duck: MagicMock, repo_root: Path, user_corpus: Path
    ) -> None:
        """Issue #84: a schema-less corpus reports skill_count 0; the
        remediation must mention `python -m agentalloy.migrate` because
        `agentalloy install-packs` alone cannot create the graph schema."""
        (user_corpus / "skills.duck").write_bytes(b"fake")
        (user_corpus / "ladybug").mkdir()
        mock_duck.return_value = {
            "skill_count": 0,
            "fragment_count": 0,
            "embedding_model": None,
            "embedding_dim": None,
        }
        result = check_corpus(repo_root)
        assert result["action"] == "missing_files"
        remediation = result["remediation"]
        assert "python -m agentalloy.migrate" in remediation
        assert "install-packs" in remediation


class TestNoNetworkCalls:
    @patch("agentalloy.install.subcommands.seed_corpus._check_duckdb")
    def test_no_http_imports(
        self, mock_duck: MagicMock, repo_root: Path, user_corpus: Path
    ) -> None:
        """seed-corpus should make zero network calls."""
        (user_corpus / "skills.duck").write_bytes(b"fake")
        (user_corpus / "ladybug").mkdir()
        mock_duck.return_value = {
            "skill_count": 93,
            "fragment_count": 1003,
            "embedding_model": "nomic-embed-text-v1.5",
            "embedding_dim": 768,
        }
        # Patch urllib to detect any network call
        with patch("urllib.request.urlopen", side_effect=AssertionError("Network call detected!")):
            result = check_corpus(repo_root)
        assert result["action"] == "verified_present"


class TestRunEntrypoint:
    """The CLI entrypoint must treat `initialized_empty` as success.

    Regression: previously fell through to `return 1`, breaking the
    fresh-install happy path documented in the docstring.
    """

    def test_initialized_empty_exits_zero(
        self, repo_root: Path, no_bundled_corpus: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from agentalloy.install import state as install_state

        rc = run(argparse.Namespace(json=True))
        captured = capsys.readouterr()
        assert rc == 0, captured.err
        payload = json.loads(captured.out)
        assert payload["action"] == "initialized_empty"
        st = install_state.load_state()
        assert install_state.is_step_completed(st, "seed-corpus")


class TestDurationTracking:
    @patch("agentalloy.install.subcommands.seed_corpus._check_duckdb")
    def test_duration_ms_present(
        self, mock_duck: MagicMock, repo_root: Path, user_corpus: Path
    ) -> None:
        (user_corpus / "skills.duck").write_bytes(b"fake")
        (user_corpus / "ladybug").mkdir()
        mock_duck.return_value = {
            "skill_count": 93,
            "fragment_count": 1003,
            "embedding_model": "nomic-embed-text-v1.5",
            "embedding_dim": 768,
        }
        result = check_corpus(repo_root)
        assert "duration_ms" in result
        assert isinstance(result["duration_ms"], int)


class TestCorpusSkillCount:
    """Shared post-install/upgrade guard seam (used by setup #261 and upgrade)."""

    def test_zero_when_corpus_absent(self, repo_root: Path, user_corpus: Path) -> None:
        # user_corpus dir exists but holds no skills.duck / ladybug
        assert corpus_skill_count() == 0

    def test_returns_skill_count_when_populated(self, user_corpus: Path) -> None:
        (user_corpus / "skills.duck").write_bytes(b"fake")
        (user_corpus / "ladybug").mkdir()
        with patch(
            "agentalloy.install.subcommands.seed_corpus._check_duckdb",
            return_value={"skill_count": 142},
        ):
            assert corpus_skill_count() == 142

    def test_zero_when_check_raises(self, user_corpus: Path) -> None:
        (user_corpus / "skills.duck").write_bytes(b"fake")
        (user_corpus / "ladybug").mkdir()
        with patch(
            "agentalloy.install.subcommands.seed_corpus._check_duckdb",
            side_effect=RuntimeError("corrupt"),
        ):
            assert corpus_skill_count() == 0
