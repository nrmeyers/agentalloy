"""Unit tests for the ``update`` subcommand."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from agentalloy.install import state as install_state
from agentalloy.install.subcommands import update as upd


@pytest.fixture()
def repo_root(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text("")
    return tmp_path


class TestCorpusPresence:
    def test_missing_corpus_warns(self, repo_root: Path) -> None:
        result = upd.update(root=repo_root)
        assert result["corpus"]["present"] is False
        assert any("seed-corpus" in w for w in result["warnings"])

    def test_present_corpus_reports_versions(self, repo_root: Path) -> None:
        user_corpus = install_state.corpus_dir()
        user_corpus.mkdir(parents=True, exist_ok=True)
        (user_corpus / "agentalloy.duck").write_text("fake")
        (user_corpus / "fragments.lance").mkdir(exist_ok=True)
        with (
            patch.object(upd, "_read_corpus_schema_version", return_value=1),
            patch.object(upd, "_expected_corpus_schema_version", return_value=1),
        ):
            result = upd.update(root=repo_root)
        assert result["corpus"]["present"] is True
        assert result["corpus"]["recorded_schema_version"] == 1
        assert result["corpus"]["expected_schema_version"] == 1


class TestSchemaDrift:
    def _setup(self, root: Path) -> None:
        # Corpus is now user-scoped (XDG_DATA_HOME/agentalloy/corpus/).
        # The conftest fixture redirects XDG dirs into tmp; this helper
        # populates fake files at the user-scoped corpus location.
        from agentalloy.install import state as install_state

        user_corpus = install_state.corpus_dir()
        user_corpus.mkdir(parents=True, exist_ok=True)
        (user_corpus / "agentalloy.duck").write_text("fake")
        (user_corpus / "fragments.lance").mkdir(exist_ok=True)

    def test_no_meta_table_warns(self, repo_root: Path) -> None:
        self._setup(repo_root)
        with patch.object(upd, "_read_corpus_schema_version", return_value=None):
            result = upd.update(root=repo_root)
        # Missing marker is harmless (implicit == current schema); the note
        # points at the rebuild that stamps it, not the bogus ingest command.
        assert any("schema_version marker" in w for w in result["warnings"])
        assert not any("agentalloy.ingest" in w for w in result["warnings"])

    def test_corpus_ahead_of_code_warns(self, repo_root: Path) -> None:
        self._setup(repo_root)
        with (
            patch.object(upd, "_read_corpus_schema_version", return_value=2),
            patch.object(upd, "_expected_corpus_schema_version", return_value=1),
        ):
            result = upd.update(root=repo_root)
        # Corpus is at a newer schema than the code expects — warn the user
        # to update the package (XDG corpus model: `pip install -U`).
        assert any("pip install" in w or "update the code" in w for w in result["warnings"])

    def test_no_migration_registered_reports_failure(self, repo_root: Path) -> None:
        self._setup(repo_root)
        with (
            patch.object(upd, "MIGRATIONS", {}),
            patch.object(upd, "_read_corpus_schema_version", return_value=1),
            patch.object(upd, "_expected_corpus_schema_version", return_value=2),
        ):
            result = upd.update(root=repo_root)
        assert result["migrations"]
        assert result["migrations"][0]["applied"] is False
        assert "No migration" in result["migrations"][0]["error"]

    def test_failed_migration_not_recorded_as_completed(self, repo_root: Path) -> None:
        """A failed migration must not be recorded as a completed update step,
        otherwise the install state lies about its corpus on next run."""
        self._setup(repo_root)
        with (
            patch.object(upd, "MIGRATIONS", {}),
            patch.object(upd, "_read_corpus_schema_version", return_value=1),
            patch.object(upd, "_expected_corpus_schema_version", return_value=2),
        ):
            upd.update(root=repo_root)
        st = install_state.load_state(repo_root)
        completed = [s["step"] for s in st.get("completed_steps", [])]
        assert "update" not in completed

    def test_registered_migration_runs(self, repo_root: Path) -> None:
        self._setup(repo_root)
        called: list[Path] = []

        def fake_mig(p: Path) -> None:
            called.append(p)

        with (
            patch.object(upd, "MIGRATIONS", {(1, 2): fake_mig}),
            patch.object(upd, "_read_corpus_schema_version", return_value=1),
            patch.object(upd, "_expected_corpus_schema_version", return_value=2),
        ):
            result = upd.update(root=repo_root)
        assert called
        assert result["migrations"][0]["applied"] is True


class TestModelDrift:
    def test_no_recommend_models_run(self, repo_root: Path) -> None:
        result = upd.update(root=repo_root)
        assert result["models"]["checked"] is False

    def test_drift_detected(self, repo_root: Path) -> None:
        # `models_pulled` stores `runner:model` strings; expected list is bare
        # model names. Drift logic must strip the runner prefix before
        # comparing.
        st = install_state.load_state(repo_root)
        st["completed_steps"] = [
            {
                "step": "recommend-models",
                "selected": {
                    "embed_model": "nomic-embed-text-v1.5.Q8_0.gguf",
                    "ingest_model": "qwen3.5:0.8b",
                },
            }
        ]
        st["models_pulled"] = ["fastflowlm:nomic-embed-text-v1.5.Q8_0.gguf"]  # ingest_model missing
        install_state.save_state(st, repo_root)
        result = upd.update(root=repo_root)
        assert result["models"]["checked"] is True
        assert "qwen3.5:0.8b" in result["models"]["drifted_models"]
        assert "pull-models" in result["models"]["remediation"]

    def test_no_drift_with_runner_prefix(self, repo_root: Path) -> None:
        """models_pulled in `runner:model` format must match against bare names."""
        st = install_state.load_state(repo_root)
        st["completed_steps"] = [
            {
                "step": "recommend-models",
                "selected": {
                    "embed_model": "nomic-embed-text-v1.5.Q8_0.gguf",
                    "ingest_model": "qwen2.5:7b",
                },
            }
        ]
        st["models_pulled"] = ["ollama:nomic-embed-text-v1.5.Q8_0.gguf", "ollama:qwen2.5:7b"]
        install_state.save_state(st, repo_root)
        result = upd.update(root=repo_root)
        assert result["models"]["drifted_models"] == []
        assert result["models"]["remediation"] is None

    def test_no_drift(self, repo_root: Path) -> None:
        st = install_state.load_state(repo_root)
        st["completed_steps"] = [
            {
                "step": "recommend-models",
                "selected": {"embed_model": "e", "ingest_model": "i"},
            }
        ]
        st["models_pulled"] = ["ollama:e", "ollama:i"]
        install_state.save_state(st, repo_root)
        result = upd.update(root=repo_root)
        assert result["models"]["drifted_models"] == []
        assert result["models"]["remediation"] is None


class TestGitStatus:
    def test_no_git_repo(self, repo_root: Path) -> None:
        result = upd.update(root=repo_root)
        assert result["git"]["is_git"] is False


class TestOutputSchema:
    def test_required_keys(self, repo_root: Path) -> None:
        result = upd.update(root=repo_root)
        for key in (
            "schema_version",
            "git",
            "corpus",
            "migrations",
            "models",
            "warnings",
            "duration_ms",
        ):
            assert key in result
