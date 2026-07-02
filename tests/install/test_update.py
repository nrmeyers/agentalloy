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

    def test_no_meta_table_warns_when_stamp_blocked(self, repo_root: Path) -> None:
        # The fake (non-DuckDB) file makes the in-place stamp fail, standing in
        # for the real blocked case: a running service holding the file open.
        self._setup(repo_root)
        with patch.object(upd, "_read_corpus_schema_version", return_value=None):
            result = upd.update(root=repo_root)
        # Missing marker is harmless (implicit == current schema); the warning
        # must not tell the user to run a command that is doomed while the
        # service holds the file (the old advice was `reembed --force`).
        assert any("schema_version marker" in w for w in result["warnings"])
        assert not any("reembed --force" in w for w in result["warnings"])
        assert not any("agentalloy.ingest" in w for w in result["warnings"])

    def test_missing_marker_is_stamped_in_place(self, repo_root: Path) -> None:
        """With the writer lock free (the upgrade flow: service stopped), a
        missing schema_version marker is stamped directly instead of deferring
        to a full corpus rebuild."""
        from agentalloy.install import state as install_state
        from agentalloy.storage.skill_store import DuckDBSkillStore

        user_corpus = install_state.corpus_dir()
        user_corpus.mkdir(parents=True, exist_ok=True)
        duck_path = user_corpus / "agentalloy.duck"
        with DuckDBSkillStore(str(duck_path)) as store:
            store.migrate()  # real schema, no schema_version marker yet
        (user_corpus / "fragments.lance").mkdir(exist_ok=True)

        result = upd.update(root=repo_root)

        assert result["corpus"]["schema_version_stamped"] is True
        expected = upd._expected_corpus_schema_version()
        assert result["corpus"]["recorded_schema_version"] == expected
        assert not any("schema_version marker" in w for w in result["warnings"])
        # The marker is durable — a second update reads it back normally.
        assert upd._read_corpus_schema_version(duck_path) == expected

    def test_stamp_blocked_by_open_handle_warns(self, repo_root: Path) -> None:
        """A concurrently held handle (the running service) blocks the brief
        stamp writer; update() falls back to the warning instead of failing."""
        from agentalloy.install import state as install_state
        from agentalloy.storage.skill_store import DuckDBSkillStore

        user_corpus = install_state.corpus_dir()
        user_corpus.mkdir(parents=True, exist_ok=True)
        duck_path = user_corpus / "agentalloy.duck"
        with DuckDBSkillStore(str(duck_path)) as store:
            store.migrate()
        (user_corpus / "fragments.lance").mkdir(exist_ok=True)

        # Hold the file read-only for the duration — DuckDB then refuses the
        # stamp's writer connection (mixed-config in-process, lock cross-process).
        holder = DuckDBSkillStore(str(duck_path), read_only=True).open()
        try:
            result = upd.update(root=repo_root)
        finally:
            holder.close()

        assert "schema_version_stamped" not in result["corpus"]
        assert any("held open" in w for w in result["warnings"])

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
