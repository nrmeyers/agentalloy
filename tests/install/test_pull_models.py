# pyright: reportPrivateUsage=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownLambdaType=false
"""Unit tests for the ``pull-models`` subcommand.

Maps to test-plan.md § Model pulling. llama-server (llama.cpp) is the sole
inference runner; the build + GGUF-download flow is what gets exercised here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from agentalloy.install.subcommands.pull_models import (
    _GGUF_URL_MAP,  # pyright: ignore[reportPrivateUsage]
    _PRESENCE_CHECKS,  # pyright: ignore[reportPrivateUsage]
    STEP_NAME,
    _collect_model_runner_pairs,  # pyright: ignore[reportPrivateUsage]
    _handle_llama_server,  # pyright: ignore[reportPrivateUsage]
    _is_model_present_llama_server,  # pyright: ignore[reportPrivateUsage]
    pull_models,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def repo_root(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text("")
    return tmp_path


def _recommend_output(
    *,
    embed_model: str = "Qwen3-Embedding-0.6B-Q8_0.gguf",
    embed_runner: str = "llama-server",
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "host_target": "CPU+RAM",
        "preset": "cpu",
        "options": [
            {
                "default": True,
                "embed_model": embed_model,
                "embed_runner": embed_runner,
            },
        ],
    }


# ---------------------------------------------------------------------------
# Pair extraction
# ---------------------------------------------------------------------------


class TestCollectPairs:
    def test_single_embed_pair(self) -> None:
        option = {
            "embed_model": "Qwen3-Embedding-0.6B-Q8_0.gguf",
            "embed_runner": "llama-server",
        }
        pairs = _collect_model_runner_pairs(option)
        assert len(pairs) == 1
        assert ("Qwen3-Embedding-0.6B-Q8_0.gguf", "llama-server") in pairs

    def test_ignores_ingest_fields_if_present(self) -> None:
        option = {
            "embed_model": "Qwen3-Embedding-0.6B-Q8_0.gguf",
            "embed_runner": "llama-server",
            "ingest_model": "qwen3.5:0.8b",
            "ingest_runner": "llama-server",
        }
        pairs = _collect_model_runner_pairs(option)
        assert len(pairs) == 1
        assert ("Qwen3-Embedding-0.6B-Q8_0.gguf", "llama-server") in pairs

    def test_deduplicates_same_model_runner(self) -> None:
        option = {
            "embed_model": "Qwen3-Embedding-0.6B-Q8_0.gguf",
            "embed_runner": "llama-server",
        }
        pairs = _collect_model_runner_pairs(option)
        assert len(pairs) == 1

    def test_collects_embed_and_rerank_pairs(self) -> None:
        option = {
            "embed_model": "Qwen3-Embedding-0.6B-Q8_0.gguf",
            "embed_runner": "llama-server",
            "rerank_model": "Qwen3-Reranker-0.6B-Q8_0.gguf",
            "rerank_runner": "llama-server",
        }
        pairs = _collect_model_runner_pairs(option)
        assert len(pairs) == 2
        assert ("Qwen3-Embedding-0.6B-Q8_0.gguf", "llama-server") in pairs
        assert ("Qwen3-Reranker-0.6B-Q8_0.gguf", "llama-server") in pairs

    def test_rerank_runner_defaults_to_embed_runner(self) -> None:
        option = {
            "embed_model": "Qwen3-Embedding-0.6B-Q8_0.gguf",
            "embed_runner": "llama-server",
            "rerank_model": "Qwen3-Reranker-0.6B-Q8_0.gguf",
        }
        pairs = _collect_model_runner_pairs(option)
        assert ("Qwen3-Reranker-0.6B-Q8_0.gguf", "llama-server") in pairs


# ---------------------------------------------------------------------------
# llama-server GGUF presence check
# ---------------------------------------------------------------------------


class TestLlamaServerPresence:
    def test_model_present_when_file_exists(self, tmp_path: Path) -> None:
        models_dir = tmp_path / "models"
        models_dir.mkdir(parents=True)
        (models_dir / "Qwen3-Embedding-0.6B-Q8_0.gguf").write_bytes(b"gguf")
        with patch(
            "agentalloy.install.subcommands.pull_models.install_state.user_data_dir",
            return_value=tmp_path,
        ):
            assert _is_model_present_llama_server("Qwen3-Embedding-0.6B-Q8_0.gguf") is True

    def test_model_absent_when_file_missing(self, tmp_path: Path) -> None:
        with patch(
            "agentalloy.install.subcommands.pull_models.install_state.user_data_dir",
            return_value=tmp_path,
        ):
            assert _is_model_present_llama_server("Qwen3-Embedding-0.6B-Q8_0.gguf") is False


# ---------------------------------------------------------------------------
# llama-server build + download orchestration
# ---------------------------------------------------------------------------


class TestHandleLlamaServer:
    def test_skips_download_when_model_present(self) -> None:
        """Binary on PATH + GGUF already downloaded → no-op (nothing pulled)."""
        with (
            patch("shutil.which", return_value="/usr/local/bin/llama-server"),
            patch(
                "agentalloy.install.subcommands.pull_models._is_model_present_llama_server",
                return_value=True,
            ),
            patch("agentalloy.install.subcommands.pull_models._download_gguf") as mock_dl,
        ):
            pulled, errors = _handle_llama_server("Qwen3-Embedding-0.6B-Q8_0.gguf", False)
        assert pulled == []
        assert errors == []
        mock_dl.assert_not_called()

    def test_downloads_when_model_missing(self) -> None:
        """Binary on PATH + GGUF missing → download and record the pull."""
        with (
            patch("shutil.which", return_value="/usr/local/bin/llama-server"),
            patch(
                "agentalloy.install.subcommands.pull_models._is_model_present_llama_server",
                return_value=False,
            ),
            patch(
                "agentalloy.install.subcommands.pull_models._download_gguf",
                return_value={"success": True, "path": "/data/models/x.gguf", "duration_ms": 42},
            ),
        ):
            pulled, errors = _handle_llama_server("Qwen3-Embedding-0.6B-Q8_0.gguf", False)
        assert errors == []
        assert len(pulled) == 1
        assert pulled[0]["runner"] == "llama-server"
        assert pulled[0]["model"] == "Qwen3-Embedding-0.6B-Q8_0.gguf"

    def test_download_failure_surfaces_error(self) -> None:
        with (
            patch("shutil.which", return_value="/usr/local/bin/llama-server"),
            patch(
                "agentalloy.install.subcommands.pull_models._is_model_present_llama_server",
                return_value=False,
            ),
            patch(
                "agentalloy.install.subcommands.pull_models._download_gguf",
                return_value={"success": False, "error": "404 Not Found"},
            ),
        ):
            pulled, errors = _handle_llama_server("Qwen3-Embedding-0.6B-Q8_0.gguf", False)
        assert pulled == []
        assert len(errors) == 1
        assert "404" in errors[0]["error"]

    def test_non_interactive_missing_binary_surfaces_actionable_error(self) -> None:
        """No binary + non-interactive → build skipped, error with manual hint."""
        with (
            patch("shutil.which", return_value=None),
            patch("agentalloy.install.subcommands.pull_models._build_llama_server") as mock_build,
        ):
            pulled, errors = _handle_llama_server("Qwen3-Embedding-0.6B-Q8_0.gguf", False)
        assert pulled == []
        assert len(errors) == 1
        assert "build was skipped" in errors[0]["error"]
        assert "git clone" in errors[0]["hint"]
        mock_build.assert_not_called()

    def test_build_failure_surfaces_error(self) -> None:
        """No binary + interactive 'y' → build attempted; failure surfaces."""
        with (
            patch("shutil.which", return_value=None),
            patch("builtins.input", return_value="y"),
            patch(
                "agentalloy.install.subcommands.pull_models._build_llama_server",
                return_value={
                    "success": False,
                    "error": "cmake not found",
                    "hint": "install cmake",
                },
            ),
        ):
            pulled, errors = _handle_llama_server("Qwen3-Embedding-0.6B-Q8_0.gguf", True)
        assert pulled == []
        assert len(errors) == 1
        assert "cmake not found" in errors[0]["error"]


# ---------------------------------------------------------------------------
# Full pull_models
# ---------------------------------------------------------------------------


class TestPullModels:
    def test_pulls_embed_model(self, repo_root: Path) -> None:
        models = _recommend_output()

        def always_false(m: str) -> bool:
            return False

        with (
            patch.dict(_PRESENCE_CHECKS, {"llama-server": always_false}),
            patch(
                "agentalloy.install.subcommands.pull_models._handle_llama_server",
                return_value=(
                    [{"runner": "llama-server", "model": "Qwen3-Embedding-0.6B-Q8_0.gguf"}],
                    [],
                ),
            ),
        ):
            result = pull_models(models, root=repo_root)
        assert result["schema_version"] == 1
        assert len(result["auto_pulled"]) == 1
        assert result["auto_pulled"][0]["model"] == "Qwen3-Embedding-0.6B-Q8_0.gguf"
        assert result["manual_steps_required"] == []
        assert result["skipped_already_present"] == []

    def test_skips_already_present(self, repo_root: Path) -> None:
        models = _recommend_output()

        def always_true(m: str) -> bool:
            return True

        with patch.dict(_PRESENCE_CHECKS, {"llama-server": always_true}):
            result = pull_models(models, root=repo_root)
        assert len(result["skipped_already_present"]) == 1
        assert result["auto_pulled"] == []

    def test_unknown_runner_becomes_manual_step(self, repo_root: Path) -> None:
        models = _recommend_output(embed_runner="vllm")

        result = pull_models(models, root=repo_root)
        assert len(result["manual_steps_required"]) == 1
        assert result["manual_steps_required"][0]["runner"] == "vllm"

    def test_idempotent_skip(self, repo_root: Path) -> None:
        """Second run returns cached result when step already completed."""
        models = _recommend_output()

        def always_true(m: str) -> bool:
            return True

        with patch.dict(_PRESENCE_CHECKS, {"llama-server": always_true}):
            pull_models(models, root=repo_root)
        # Second call should return cached result (no longer raises SystemExit)
        cached = pull_models(models, root=repo_root)
        assert cached is not None
        assert "auto_pulled" in cached

    def test_records_state(self, repo_root: Path) -> None:
        models = _recommend_output()

        def always_true(m: str) -> bool:
            return True

        with patch.dict(_PRESENCE_CHECKS, {"llama-server": always_true}):
            pull_models(models, root=repo_root)
        from agentalloy.install.state import is_step_completed, load_state

        st = load_state(repo_root)
        assert is_step_completed(st, STEP_NAME)

    def test_no_options_exits(self, repo_root: Path) -> None:
        with pytest.raises(SystemExit):
            pull_models({"options": []}, root=repo_root)

    def test_pull_error_recorded(self, repo_root: Path) -> None:
        models = _recommend_output()

        def always_false(m: str) -> bool:
            return False

        with (
            patch.dict(_PRESENCE_CHECKS, {"llama-server": always_false}),
            patch(
                "agentalloy.install.subcommands.pull_models._handle_llama_server",
                return_value=(
                    [],
                    [
                        {
                            "runner": "llama-server",
                            "model": "Qwen3-Embedding-0.6B-Q8_0.gguf",
                            "success": False,
                            "error": "download failed",
                        }
                    ],
                ),
            ),
        ):
            result = pull_models(models, root=repo_root)
        assert "errors" in result
        assert len(result["errors"]) == 1

    def test_partial_failure_does_not_record_completion(self, repo_root: Path) -> None:
        """If any pull fails, pull-models must NOT mark itself completed —
        otherwise idempotency permanently skips it on rerun."""
        from agentalloy.install.state import is_step_completed, load_state

        models = _recommend_output()

        def always_false(m: str) -> bool:
            return False

        with (
            patch.dict(_PRESENCE_CHECKS, {"llama-server": always_false}),
            patch(
                "agentalloy.install.subcommands.pull_models._handle_llama_server",
                return_value=(
                    [],
                    [
                        {
                            "runner": "llama-server",
                            "model": "Qwen3-Embedding-0.6B-Q8_0.gguf",
                            "success": False,
                            "error": "download failed",
                        }
                    ],
                ),
            ),
        ):
            pull_models(models, root=repo_root)
        st = load_state(repo_root)
        assert not is_step_completed(st, "pull-models")
        # And no models_pulled tracking either, since nothing succeeded.
        assert not st.get("models_pulled")

    def test_models_pulled_in_state(self, repo_root: Path) -> None:
        models = _recommend_output()

        def always_false(m: str) -> bool:
            return False

        with (
            patch.dict(_PRESENCE_CHECKS, {"llama-server": always_false}),
            patch(
                "agentalloy.install.subcommands.pull_models._handle_llama_server",
                return_value=(
                    [{"runner": "llama-server", "model": "Qwen3-Embedding-0.6B-Q8_0.gguf"}],
                    [],
                ),
            ),
        ):
            pull_models(models, root=repo_root)
        from agentalloy.install.state import load_state

        st = load_state(repo_root)
        assert "llama-server:Qwen3-Embedding-0.6B-Q8_0.gguf" in st["models_pulled"]


class TestRunExitCodes:
    """``_run`` exit codes: 0 = work done, 4 = no-op, 1 = error."""

    def _models_file(self, repo_root: Path, runner: str = "llama-server") -> Path:
        import json as _json

        p = repo_root / "models.json"
        p.write_text(_json.dumps(_recommend_output(embed_runner=runner)))
        return p

    def test_exit_4_when_all_models_already_present(self, repo_root: Path) -> None:
        """Re-running pull-models after install: every model is present.
        No pulls happen, no manual steps required → EXIT_NOOP (4)."""
        from argparse import Namespace

        from agentalloy.install.subcommands.pull_models import _run

        models_path = self._models_file(repo_root)

        def always_present(m: str) -> bool:
            return True

        with patch.dict(_PRESENCE_CHECKS, {"llama-server": always_present}):
            rc = _run(Namespace(models=str(models_path), runner=None, quiet=True))
        assert rc == 4

    def test_exit_0_when_models_pulled(self, repo_root: Path) -> None:
        from argparse import Namespace

        from agentalloy.install.subcommands.pull_models import _run

        models_path = self._models_file(repo_root)

        def always_absent(m: str) -> bool:
            return False

        with (
            patch.dict(_PRESENCE_CHECKS, {"llama-server": always_absent}),
            patch(
                "agentalloy.install.subcommands.pull_models._handle_llama_server",
                return_value=(
                    [{"runner": "llama-server", "model": "Qwen3-Embedding-0.6B-Q8_0.gguf"}],
                    [],
                ),
            ),
        ):
            rc = _run(Namespace(models=str(models_path), runner=None, quiet=True))
        assert rc == 0

    def test_exit_1_when_pull_fails(self, repo_root: Path) -> None:
        from argparse import Namespace

        from agentalloy.install.subcommands.pull_models import _run

        models_path = self._models_file(repo_root)

        def always_absent(m: str) -> bool:
            return False

        with (
            patch.dict(_PRESENCE_CHECKS, {"llama-server": always_absent}),
            patch(
                "agentalloy.install.subcommands.pull_models._handle_llama_server",
                return_value=(
                    [],
                    [
                        {
                            "runner": "llama-server",
                            "model": "Qwen3-Embedding-0.6B-Q8_0.gguf",
                            "success": False,
                            "error": "download failed",
                        }
                    ],
                ),
            ),
        ):
            rc = _run(Namespace(models=str(models_path), runner=None, quiet=True))
        assert rc == 1


# ---------------------------------------------------------------------------
# GGUF download map — must include BOTH the embed and reranker GGUFs so a
# single install pull provisions both llama-server instances.
# ---------------------------------------------------------------------------


class TestGgufUrlMap:
    def test_embed_and_reranker_ggufs_present(self) -> None:
        assert "Qwen3-Embedding-0.6B-Q8_0.gguf" in _GGUF_URL_MAP
        assert "Qwen3-Reranker-0.6B-Q8_0.gguf" in _GGUF_URL_MAP
        for url in _GGUF_URL_MAP.values():
            assert url.startswith("https://huggingface.co/")
