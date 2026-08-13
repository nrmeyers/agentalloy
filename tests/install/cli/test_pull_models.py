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
    _PREBUILT_ASSET_SUFFIX,  # pyright: ignore[reportPrivateUsage]
    _PRESENCE_CHECKS,  # pyright: ignore[reportPrivateUsage]
    STEP_NAME,
    _collect_model_runner_pairs,  # pyright: ignore[reportPrivateUsage]
    _detect_prebuilt_platform,  # pyright: ignore[reportPrivateUsage]
    _ensure_llama_server_binary,  # pyright: ignore[reportPrivateUsage]
    _extract_archive,  # pyright: ignore[reportPrivateUsage]
    _find_llama_server_binary,  # pyright: ignore[reportPrivateUsage]
    _handle_llama_server,  # pyright: ignore[reportPrivateUsage]
    _is_model_present_llama_server,  # pyright: ignore[reportPrivateUsage]
    _prebuilt_asset,  # pyright: ignore[reportPrivateUsage]
    _write_llama_server_wrapper,  # pyright: ignore[reportPrivateUsage]
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
                "agentalloy.install.subcommands.pull_models._llama_server_runs",
                return_value=True,
            ),
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
                "agentalloy.install.subcommands.pull_models._llama_server_runs",
                return_value=True,
            ),
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
                "agentalloy.install.subcommands.pull_models._llama_server_runs",
                return_value=True,
            ),
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

    def test_missing_binary_downloads_prebuilt_then_gguf(self) -> None:
        """No binary → prebuilt download (works headlessly) → then GGUF pull."""
        with (
            patch("shutil.which", return_value=None),
            patch(
                "agentalloy.install.subcommands.pull_models._download_prebuilt_llama_server",
                return_value={"success": True, "binary_path": "/home/u/.local/bin/llama-server"},
            ) as mock_prebuilt,
            patch(
                "agentalloy.install.subcommands.pull_models._is_model_present_llama_server",
                return_value=False,
            ),
            patch(
                "agentalloy.install.subcommands.pull_models._download_gguf",
                return_value={"success": True, "path": "/data/models/x.gguf", "duration_ms": 7},
            ),
        ):
            pulled, errors = _handle_llama_server("Qwen3-Embedding-0.6B-Q8_0.gguf", False)
        assert errors == []
        assert len(pulled) == 1
        mock_prebuilt.assert_called_once()

    def test_non_interactive_prebuilt_failure_surfaces_error_no_build(self) -> None:
        """No binary + non-interactive + prebuilt fails → error, NO source build."""
        with (
            patch("shutil.which", return_value=None),
            patch(
                "agentalloy.install.subcommands.pull_models._download_prebuilt_llama_server",
                return_value={
                    "success": False,
                    "binary_path": None,
                    "error": "no prebuilt asset for this platform (sunos/sparc)",
                    "hint": "build manually",
                },
            ),
            patch("agentalloy.install.subcommands.pull_models._build_llama_server") as mock_build,
        ):
            pulled, errors = _handle_llama_server("Qwen3-Embedding-0.6B-Q8_0.gguf", False)
        assert pulled == []
        assert len(errors) == 1
        assert "prebuilt download failed" in errors[0]["error"]
        mock_build.assert_not_called()

    def test_interactive_build_fallback_failure_surfaces_error(self) -> None:
        """No binary + prebuilt fails + interactive 'y' → source build attempted."""
        with (
            patch("shutil.which", return_value=None),
            patch(
                "agentalloy.install.subcommands.pull_models._download_prebuilt_llama_server",
                return_value={"success": False, "binary_path": None, "error": "404", "hint": "h"},
            ),
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
    @pytest.fixture(autouse=True)
    def _stub_llama_binary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # These tests exercise the pull *orchestration* (state recording, exit
        # codes), not binary provisioning. Stub the binary step so they don't do
        # a real probe/prebuilt-download — slow, network-bound, and racy under
        # -n auto (concurrent downloads collide). Binary provisioning is covered
        # by TestEnsureLlamaServerBinary / TestHandleLlamaServer.
        import shutil

        real_which = shutil.which
        monkeypatch.setattr(
            shutil,
            "which",
            lambda name, *a, **k: (
                "/usr/bin/llama-server" if name == "llama-server" else real_which(name, *a, **k)
            ),
        )
        monkeypatch.setattr(
            "agentalloy.install.subcommands.pull_models._llama_server_runs",
            lambda *a, **k: True,
        )

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

        # "Already present" for llama-server now requires the GGUF (presence check)
        # AND a runner binary that's on PATH *and actually runs* (a stale wrapper
        # doesn't count), so mock shutil.which + _llama_server_runs.
        with (
            patch.dict(_PRESENCE_CHECKS, {"llama-server": always_true}),
            patch(
                "agentalloy.install.subcommands.pull_models.shutil.which",
                return_value="/usr/bin/llama-server",
            ),
            patch(
                "agentalloy.install.subcommands.pull_models._llama_server_runs",
                return_value=True,
            ),
        ):
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

    @pytest.fixture(autouse=True)
    def _stub_llama_binary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Orchestration/exit-code tests: stub binary provisioning (see
        # TestPullModels._stub_llama_binary) so no real probe/download runs.
        import shutil

        real_which = shutil.which
        monkeypatch.setattr(
            shutil,
            "which",
            lambda name, *a, **k: (
                "/usr/bin/llama-server" if name == "llama-server" else real_which(name, *a, **k)
            ),
        )
        monkeypatch.setattr(
            "agentalloy.install.subcommands.pull_models._llama_server_runs",
            lambda *a, **k: True,
        )

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


# ---------------------------------------------------------------------------
# Binary provisioning — prebuilt download is the default; from-source build is
# an interactive-only fallback.
# ---------------------------------------------------------------------------


class TestEnsureLlamaServerBinary:
    def test_uses_binary_on_path(self) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/llama-server"),
            patch(
                "agentalloy.install.subcommands.pull_models._llama_server_runs",
                return_value=True,
            ),
            patch(
                "agentalloy.install.subcommands.pull_models._llama_server_runs",
                return_value=True,
            ),
            patch(
                "agentalloy.install.subcommands.pull_models._download_prebuilt_llama_server"
            ) as prebuilt,
        ):
            res = _ensure_llama_server_binary(False)
        assert res["success"] is True
        assert res["binary_path"] == "/usr/bin/llama-server"
        prebuilt.assert_not_called()

    def test_downloads_prebuilt_when_absent(self) -> None:
        with (
            patch("shutil.which", return_value=None),
            patch(
                "agentalloy.install.subcommands.pull_models._download_prebuilt_llama_server",
                return_value={"success": True, "binary_path": "/h/.local/bin/llama-server"},
            ),
        ):
            res = _ensure_llama_server_binary(False)
        assert res["success"] is True

    def test_non_interactive_no_prebuilt_no_build(self) -> None:
        with (
            patch("shutil.which", return_value=None),
            patch(
                "agentalloy.install.subcommands.pull_models._download_prebuilt_llama_server",
                return_value={"success": False, "error": "no asset", "hint": "h"},
            ),
            patch("agentalloy.install.subcommands.pull_models._build_llama_server") as build,
        ):
            res = _ensure_llama_server_binary(False)
        assert res["success"] is False
        assert "prebuilt download failed" in res["error"]
        build.assert_not_called()

    def test_interactive_declines_build(self) -> None:
        with (
            patch("shutil.which", return_value=None),
            patch(
                "agentalloy.install.subcommands.pull_models._download_prebuilt_llama_server",
                return_value={"success": False, "error": "no asset", "hint": "h"},
            ),
            patch("builtins.input", return_value="n"),
            patch("agentalloy.install.subcommands.pull_models._build_llama_server") as build,
        ):
            res = _ensure_llama_server_binary(True)
        assert res["success"] is False
        build.assert_not_called()

    def test_interactive_accepts_build(self) -> None:
        with (
            patch("shutil.which", return_value=None),
            patch(
                "agentalloy.install.subcommands.pull_models._download_prebuilt_llama_server",
                return_value={"success": False, "error": "no asset", "hint": "h"},
            ),
            patch("builtins.input", return_value="y"),
            patch(
                "agentalloy.install.subcommands.pull_models._build_llama_server",
                return_value={"success": True, "binary_path": "/h/.local/bin/llama-server"},
            ) as build,
        ):
            res = _ensure_llama_server_binary(True)
        assert res["success"] is True
        build.assert_called_once()


class TestPrebuiltAssetResolution:
    """The exact-suffix match is load-bearing: it must never select an
    accelerated (vulkan/rocm/sycl/cuda) asset for a plain-CPU install."""

    _CASES = [
        ("linux", "x86_64", "linux", "ubuntu-x64.tar.gz"),
        ("linux", "aarch64", "linux", "ubuntu-arm64.tar.gz"),
        ("darwin", "arm64", "darwin", "macos-arm64.tar.gz"),
        ("darwin", "x86_64", "darwin", "macos-x64.tar.gz"),
        ("win32", "AMD64", "win", "win-cpu-x64.zip"),
        ("win32", "ARM64", "win", "win-cpu-arm64.zip"),
    ]

    @pytest.mark.parametrize(("sysplat", "machine", "os_key", "suffix"), _CASES)
    def test_exact_asset_for_platform(
        self, sysplat: str, machine: str, os_key: str, suffix: str
    ) -> None:
        with (
            patch("agentalloy.install.subcommands.pull_models.sys.platform", sysplat),
            patch(
                "agentalloy.install.subcommands.pull_models.platform.machine",
                return_value=machine,
            ),
        ):
            resolved = _prebuilt_asset()
        assert resolved is not None
        got_os, asset, url = resolved
        assert got_os == os_key
        assert asset.endswith(suffix)
        assert url.endswith(asset)
        # Never an accelerated variant.
        for bad in ("vulkan", "rocm", "sycl", "cuda", "hip", "openvino"):
            assert bad not in asset

    def test_unsupported_os_returns_none(self) -> None:
        with (
            patch("agentalloy.install.subcommands.pull_models.sys.platform", "sunos5"),
            patch(
                "agentalloy.install.subcommands.pull_models.platform.machine",
                return_value="x86_64",
            ),
        ):
            assert _detect_prebuilt_platform() is None
            assert _prebuilt_asset() is None

    def test_unsupported_arch_returns_none(self) -> None:
        with (
            patch("agentalloy.install.subcommands.pull_models.sys.platform", "linux"),
            patch(
                "agentalloy.install.subcommands.pull_models.platform.machine",
                return_value="s390x",
            ),
        ):
            assert _detect_prebuilt_platform() is None

    def test_all_mapped_suffixes_are_plain_cpu(self) -> None:
        for suffix in _PREBUILT_ASSET_SUFFIX.values():
            for bad in ("vulkan", "rocm", "sycl", "cuda", "hip", "openvino"):
                assert bad not in suffix


def _make_fake_toolchain_tar(dest: Path, top: str = "llama-bTEST") -> Path:
    """Build a tiny tar.gz mimicking a release: top/llama-server + top/lib*.so."""
    import io
    import tarfile as _tarfile

    archive = dest / "llama-bTEST-bin-ubuntu-x64.tar.gz"
    with _tarfile.open(archive, "w:gz") as tf:
        for name, data in (
            (f"{top}/llama-server", b"#!/bin/sh\necho stub\n"),
            (f"{top}/libggml.so", b"\x00fakelib"),
        ):
            info = _tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return archive


class TestExtractAndWrapper:
    def test_extract_and_find_binary(self, tmp_path: Path) -> None:
        archive = _make_fake_toolchain_tar(tmp_path)
        out = tmp_path / "out"
        _extract_archive(archive, out)
        binary = _find_llama_server_binary(out)
        assert binary is not None
        assert binary.name == "llama-server"
        assert (binary.parent / "libggml.so").exists()

    def test_find_binary_none_when_absent(self, tmp_path: Path) -> None:
        (tmp_path / "readme.txt").write_text("nothing here")
        assert _find_llama_server_binary(tmp_path) is None

    def test_posix_wrapper_sets_library_path(self, tmp_path: Path) -> None:
        runtime = tmp_path / "rt"
        bin_dir = tmp_path / "bin"
        runtime.mkdir()
        bin_dir.mkdir()
        wrapper = _write_llama_server_wrapper("linux", runtime, bin_dir)
        assert wrapper.name == "llama-server"
        text = wrapper.read_text()
        assert "LD_LIBRARY_PATH" in text
        assert "DYLD_LIBRARY_PATH" in text
        assert str(runtime) in text
        assert text.splitlines()[0] == "#!/bin/sh"
        assert oct(wrapper.stat().st_mode)[-3:] == "755"

    def test_windows_wrapper_is_cmd(self, tmp_path: Path) -> None:
        runtime = tmp_path / "rt"
        bin_dir = tmp_path / "bin"
        runtime.mkdir()
        bin_dir.mkdir()
        wrapper = _write_llama_server_wrapper("win", runtime, bin_dir)
        assert wrapper.name == "llama-server.cmd"
        text = wrapper.read_text()
        assert 'set "PATH=' in text
        assert "llama-server.exe" in text


class TestDownloadPrebuilt:
    def test_happy_path_installs_toolchain_and_wrapper(self, tmp_path: Path) -> None:
        from agentalloy.install.subcommands import pull_models as pm

        runtime = tmp_path / "runtime" / "llama.cpp"
        bin_dir = tmp_path / "bin"
        fixture = _make_fake_toolchain_tar(tmp_path)

        def fake_dl(url: str, dest_path: Path, *, label: str = "x") -> dict[str, Any]:
            import shutil as _shutil

            _shutil.copy2(fixture, dest_path)
            return {"success": True, "error": None, "duration_ms": 1}

        with (
            patch.object(pm, "_LLAMA_CPP_RUNTIME_DIR", runtime),
            patch.object(pm, "_LLAMA_SERVER_BIN_DIR", bin_dir),
            patch.object(
                pm,
                "_prebuilt_asset",
                return_value=("linux", "llama-bTEST-bin-ubuntu-x64.tar.gz", "http://x/a.tar.gz"),
            ),
            patch.object(pm, "_download_with_retry", side_effect=fake_dl),
        ):
            res = pm._download_prebuilt_llama_server()

        assert res["success"] is True
        assert (runtime / "llama-server").exists()
        assert (runtime / "libggml.so").exists()
        wrapper = bin_dir / "llama-server"
        assert wrapper.exists()
        assert str(runtime) in wrapper.read_text()
        # Archive + staging cleaned up.
        assert not (runtime.parent / "llama-bTEST-bin-ubuntu-x64.tar.gz").exists()
        assert not (runtime.parent / "llama-bTEST-bin-ubuntu-x64.tar.gz.extract").exists()

    def test_unsupported_platform_fails_with_hint(self, tmp_path: Path) -> None:
        from agentalloy.install.subcommands import pull_models as pm

        with patch.object(pm, "_prebuilt_asset", return_value=None):
            res = pm._download_prebuilt_llama_server()
        assert res["success"] is False
        assert "no prebuilt llama-server asset" in res["error"]
        assert res["hint"]

    def test_download_failure_propagates(self, tmp_path: Path) -> None:
        from agentalloy.install.subcommands import pull_models as pm

        runtime = tmp_path / "runtime" / "llama.cpp"
        bin_dir = tmp_path / "bin"
        with (
            patch.object(pm, "_LLAMA_CPP_RUNTIME_DIR", runtime),
            patch.object(pm, "_LLAMA_SERVER_BIN_DIR", bin_dir),
            patch.object(
                pm,
                "_prebuilt_asset",
                return_value=("linux", "llama-bTEST-bin-ubuntu-x64.tar.gz", "http://x/a.tar.gz"),
            ),
            patch.object(
                pm,
                "_download_with_retry",
                return_value={"success": False, "error": "404 Not Found", "duration_ms": 0},
            ),
        ):
            res = pm._download_prebuilt_llama_server()
        assert res["success"] is False
        assert "prebuilt download failed" in res["error"]


class TestGgufUrlMap:
    def test_embed_and_reranker_ggufs_present(self) -> None:
        assert "Qwen3-Embedding-0.6B-Q8_0.gguf" in _GGUF_URL_MAP
        assert "Qwen3-Reranker-0.6B-Q8_0.gguf" in _GGUF_URL_MAP
        for url in _GGUF_URL_MAP.values():
            assert url.startswith("https://huggingface.co/")


# ---------------------------------------------------------------------------
# GGUF download resilience — transient TLS/network flakes must be retried with
# backoff (HuggingFace occasionally drops the connection mid-stream).
# ---------------------------------------------------------------------------


class TestDownloadGgufRetry:
    _MODEL = "Qwen3-Embedding-0.6B-Q8_0.gguf"

    def test_succeeds_first_attempt_no_sleep(self, tmp_path: Path) -> None:
        from agentalloy.install.subcommands import pull_models as pm

        with (
            patch.object(pm.install_state, "user_data_dir", return_value=tmp_path),
            patch.object(pm, "_download_gguf_once", return_value=123) as once,
            patch.object(pm.time, "sleep") as sleep,
        ):
            result = pm._download_gguf(self._MODEL)
        assert result["success"] is True
        assert result["path"].endswith(self._MODEL)
        assert once.call_count == 1
        sleep.assert_not_called()

    def test_retries_then_succeeds(self, tmp_path: Path) -> None:
        """Two transient failures, then success → 3 attempts, 2 backoff sleeps."""
        import urllib.error

        from agentalloy.install.subcommands import pull_models as pm

        attempts: list[int] = []

        def flaky(url: str, dest_path: Path) -> int:
            attempts.append(1)
            if len(attempts) < 3:
                # Write a partial file so the cleanup path is exercised.
                dest_path.write_bytes(b"partial")
                raise urllib.error.URLError("unexpected eof while reading")
            # Success: write the full file (stands in for the real stream).
            dest_path.write_bytes(b"complete")
            return 999

        with (
            patch.object(pm.install_state, "user_data_dir", return_value=tmp_path),
            patch.object(pm, "_download_gguf_once", side_effect=flaky),
            patch.object(pm.time, "sleep") as sleep,
        ):
            result = pm._download_gguf(self._MODEL)
        assert result["success"] is True
        assert len(attempts) == 3
        assert sleep.call_count == 2  # backoff between the 3 attempts
        # The file written on the successful attempt survives (not cleaned up).
        assert (tmp_path / "models" / self._MODEL).read_bytes() == b"complete"

    def test_exhausts_attempts_then_fails(self, tmp_path: Path) -> None:
        """All attempts fail → _DOWNLOAD_MAX_ATTEMPTS tries, partial cleaned, error surfaced."""
        import urllib.error

        from agentalloy.install.subcommands import pull_models as pm

        def always_fail(url: str, dest_path: Path) -> int:
            dest_path.write_bytes(b"partial")
            raise urllib.error.URLError("TLS connect error")

        with (
            patch.object(pm.install_state, "user_data_dir", return_value=tmp_path),
            patch.object(pm, "_download_gguf_once", side_effect=always_fail) as once,
            patch.object(pm.time, "sleep") as sleep,
        ):
            result = pm._download_gguf(self._MODEL)
        assert result["success"] is False
        assert once.call_count == pm._DOWNLOAD_MAX_ATTEMPTS
        assert sleep.call_count == pm._DOWNLOAD_MAX_ATTEMPTS - 1
        assert "after" in result["error"] and "attempts" in result["error"]
        # Partial download removed between/after attempts.
        assert not (tmp_path / "models" / self._MODEL).exists()

    def test_unknown_model_no_retry(self, tmp_path: Path) -> None:
        from agentalloy.install.subcommands import pull_models as pm

        with (
            patch.object(pm.install_state, "user_data_dir", return_value=tmp_path),
            patch.object(pm, "_download_gguf_once") as once,
        ):
            result = pm._download_gguf("does-not-exist.gguf")
        assert result["success"] is False
        once.assert_not_called()


class TestDownloadHeaders:
    """Header construction + Retry-After parsing for the shared download path."""

    def test_user_agent_always_present(self) -> None:
        from agentalloy.install.subcommands import pull_models as pm

        h = pm._download_headers("https://huggingface.co/nomic-ai/m.gguf")
        assert "agentalloy" in h["User-Agent"]

    def test_hf_token_only_on_huggingface_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agentalloy.install.subcommands import pull_models as pm

        monkeypatch.setenv("HF_TOKEN", "secret-tok")
        hf = pm._download_headers("https://huggingface.co/nomic-ai/m.gguf")
        assert hf["Authorization"] == "Bearer secret-tok"
        # Never leak the token to the GitHub-hosted binary/cudart archives.
        gh = pm._download_headers("https://github.com/ggml-org/llama.cpp/releases/x.tar.gz")
        assert "Authorization" not in gh

    def test_no_auth_header_without_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agentalloy.install.subcommands import pull_models as pm

        monkeypatch.delenv("HF_TOKEN", raising=False)
        monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)
        h = pm._download_headers("https://huggingface.co/nomic-ai/m.gguf")
        assert "Authorization" not in h

    def test_retry_after_honored_and_capped(self) -> None:
        from agentalloy.install.subcommands import pull_models as pm

        class _Exc(Exception):
            headers = {"Retry-After": "12"}

        assert pm._retry_after_seconds(_Exc(), default=5.0) == 12.0

        class _Big(Exception):
            headers = {"Retry-After": "9999"}

        assert pm._retry_after_seconds(_Big(), default=5.0) == pm._RETRY_AFTER_CAP_S

    def test_retry_after_falls_back_on_missing_or_date(self) -> None:
        from agentalloy.install.subcommands import pull_models as pm

        class _NoHeaders(Exception):
            pass

        assert pm._retry_after_seconds(_NoHeaders(), default=7.0) == 7.0

        # HTTP-date form is intentionally not parsed — fall back to our backoff.
        class _DateForm(Exception):
            headers = {"Retry-After": "Wed, 21 Oct 2025 07:28:00 GMT"}

        assert pm._retry_after_seconds(_DateForm(), default=7.0) == 7.0


class TestGpuProvisioning:
    """GPU-capable llama-server asset selection + device probe."""

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("nvidia", "nvidia"),
            ("CUDA", "nvidia"),
            ("radeon", "radeon"),
            ("amd", "radeon"),
            ("rocm", "radeon"),
            ("apple-silicon", "apple-silicon"),
            ("metal", "apple-silicon"),
            ("cpu", "cpu"),
            ("", "cpu"),
            ("nonsense", "cpu"),
            (None, "cpu"),
        ],
    )
    def test_normalize_hardware(self, value: object, expected: str) -> None:
        from agentalloy.install.subcommands import pull_models as pm

        assert pm._normalize_hardware(value) == expected

    @pytest.mark.parametrize(
        "asset,backend",
        [
            ("llama-b9631-bin-ubuntu-vulkan-x64.tar.gz", "vulkan"),
            ("llama-b9631-bin-win-cuda-13.3-x64.zip", "cuda"),
            ("llama-b9631-bin-win-hip-radeon-x64.zip", "hip"),
            ("llama-b9631-bin-ubuntu-rocm-7.2-x64.tar.gz", "rocm"),
            ("llama-b9631-bin-ubuntu-x64.tar.gz", "cpu"),
        ],
    )
    def test_asset_backend(self, asset: str, backend: str) -> None:
        from agentalloy.install.subcommands import pull_models as pm

        assert pm._asset_backend(asset) == backend

    @pytest.mark.parametrize(
        "hardware,sysplat,machine,must_contain,must_not_contain",
        [
            # Linux nvidia/radeon -> Vulkan (cross-vendor, driver-only).
            ("nvidia", "linux", "x86_64", "ubuntu-vulkan-x64", "cpu"),
            ("radeon", "linux", "x86_64", "ubuntu-vulkan-x64", "cpu"),
            ("nvidia", "linux", "aarch64", "ubuntu-vulkan-arm64", "x64"),
            # Windows nvidia -> CUDA, radeon -> HIP.
            ("nvidia", "win32", "AMD64", "win-cuda", "vulkan"),
            ("radeon", "win32", "AMD64", "win-hip", "cuda"),
            # CPU and apple-silicon take the plain asset (Metal ships in macos-arm64).
            ("cpu", "linux", "x86_64", "ubuntu-x64", "vulkan"),
            ("apple-silicon", "darwin", "arm64", "macos-arm64", "vulkan"),
            # GPU target with no GPU asset for the OS/arch -> CPU fallback.
            ("nvidia", "win32", "ARM64", "win-cpu-arm64", "cuda"),
        ],
    )
    def test_prebuilt_asset_gpu_selection(
        self,
        hardware: str,
        sysplat: str,
        machine: str,
        must_contain: str,
        must_not_contain: str,
    ) -> None:
        from agentalloy.install.subcommands import pull_models as pm

        with (
            patch("agentalloy.install.subcommands.pull_models.sys.platform", sysplat),
            patch(
                "agentalloy.install.subcommands.pull_models.platform.machine",
                return_value=machine,
            ),
        ):
            resolved = pm._prebuilt_asset(hardware)
        assert resolved is not None
        _os, asset, url = resolved
        assert must_contain in asset
        assert must_not_contain not in asset
        assert url.endswith(asset)

    def test_probe_gpu_devices_parses_list(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock

        from agentalloy.install.subcommands import pull_models as pm

        out = (
            "Available devices:\n"
            "  Vulkan0: NVIDIA GeForce RTX 3090 (24822 MiB, 2154 MiB free)\n"
            "  Vulkan1: NVIDIA GeForce RTX 3060 (12534 MiB, 3688 MiB free)\n"
        )
        with patch.object(pm.subprocess, "run", return_value=MagicMock(stdout=out, stderr="")):
            devices = pm._probe_gpu_devices(tmp_path / "llama-server")
        assert len(devices) == 2
        assert devices[0].startswith("Vulkan0:")

    def test_probe_gpu_devices_parses_metal_mtl(self, tmp_path: Path) -> None:
        """Apple Silicon: llama.cpp names its Metal device `MTL0`, not `Metal0`.

        Regression: the probe regex only matched `Metal\\d+:`, so Apple Silicon
        was wrongly reported as "no GPU device" → false CPU-only / inert `-ngl`
        warning on macOS native installs.
        """
        from unittest.mock import MagicMock

        from agentalloy.install.subcommands import pull_models as pm

        out = "Available devices:\n  MTL0: Apple M2 Pro (21845 MiB, 21845 MiB free)\n"
        with patch.object(pm.subprocess, "run", return_value=MagicMock(stdout=out, stderr="")):
            devices = pm._probe_gpu_devices(tmp_path / "llama-server")
        assert len(devices) == 1
        assert devices[0].startswith("MTL0:")

    def test_probe_gpu_devices_parses_bare_metal(self, tmp_path: Path) -> None:
        """Older single-device builds emit a bare `Metal:` with no index."""
        from unittest.mock import MagicMock

        from agentalloy.install.subcommands import pull_models as pm

        out = "Available devices:\n  Metal: Apple M1 (10922 MiB, 10922 MiB free)\n"
        with patch.object(pm.subprocess, "run", return_value=MagicMock(stdout=out, stderr="")):
            devices = pm._probe_gpu_devices(tmp_path / "llama-server")
        assert len(devices) == 1
        assert devices[0].startswith("Metal:")

    def test_probe_gpu_devices_empty_on_cpu_only(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock

        from agentalloy.install.subcommands import pull_models as pm

        with patch.object(
            pm.subprocess,
            "run",
            return_value=MagicMock(stdout="Available devices:\n  (none)\n", stderr=""),
        ):
            assert pm._probe_gpu_devices(tmp_path / "llama-server") == []

    def test_handle_llama_server_threads_hardware(self) -> None:
        from agentalloy.install.subcommands import pull_models as pm

        with patch.object(
            pm, "_ensure_llama_server_binary", return_value={"success": False, "error": "x"}
        ) as ensure:
            pm._handle_llama_server("nomic-embed-text-v1.5.Q8_0.gguf", False, "nvidia")
        ensure.assert_called_once_with(False, "nvidia")

    def test_ensure_runner_binary_normalizes_preset_and_delegates(self) -> None:
        """Public entry normalizes the preset, then delegates to the provisioner."""
        from agentalloy.install.subcommands import pull_models as pm

        with patch.object(
            pm,
            "_ensure_llama_server_binary",
            return_value={"success": True, "binary_path": "/x/llama-server", "error": None},
        ) as ensure:
            out = pm.ensure_runner_binary(interactive=False, preset="cuda")
        assert out["success"] is True
        # "cuda" preset normalizes to the "nvidia" asset target before delegating.
        ensure.assert_called_once_with(False, "nvidia")


class TestStaleWrapperReprovision:
    """A broken on-PATH llama-server (wrapper whose target was wiped) must trigger
    re-provisioning, not be trusted as present."""

    def test_llama_server_runs_true_on_zero_exit(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock

        from agentalloy.install.subcommands import pull_models as pm

        with patch.object(pm.subprocess, "run", return_value=MagicMock(returncode=0)):
            assert pm._llama_server_runs(tmp_path / "llama-server") is True

    def test_llama_server_runs_false_on_nonzero_exit(self, tmp_path: Path) -> None:
        # A wrapper whose `exec` target is missing: /bin/sh exits 127, not an exception.
        from unittest.mock import MagicMock

        from agentalloy.install.subcommands import pull_models as pm

        with patch.object(pm.subprocess, "run", return_value=MagicMock(returncode=127)):
            assert pm._llama_server_runs(tmp_path / "llama-server") is False

    def test_llama_server_runs_false_on_oserror(self, tmp_path: Path) -> None:
        from agentalloy.install.subcommands import pull_models as pm

        with patch.object(pm.subprocess, "run", side_effect=OSError("boom")):
            assert pm._llama_server_runs(tmp_path / "llama-server") is False

    def test_ensure_uses_existing_when_it_runs(self) -> None:
        """A working on-PATH binary is used as-is; no download."""
        from agentalloy.install.subcommands import pull_models as pm

        with (
            patch.object(pm.shutil, "which", return_value="/usr/bin/llama-server"),
            patch.object(pm, "_llama_server_runs", return_value=True),
            patch.object(pm, "_probe_gpu_devices", return_value=["CUDA0: RTX"]),
            patch.object(pm, "_download_prebuilt_llama_server") as dl,
        ):
            out = pm._ensure_llama_server_binary(interactive=False, hardware="nvidia")
        assert out["success"] is True
        assert out["binary_path"] == "/usr/bin/llama-server"
        dl.assert_not_called()

    def test_ensure_reprovisions_when_existing_is_broken(self) -> None:
        """A stale wrapper that resolves but won't exec triggers a re-download."""
        from agentalloy.install.subcommands import pull_models as pm

        with (
            patch.object(pm.shutil, "which", return_value="/home/u/.local/bin/llama-server"),
            patch.object(pm, "_llama_server_runs", return_value=False),
            patch.object(
                pm,
                "_download_prebuilt_llama_server",
                return_value={"success": True, "binary_path": "/runtime/llama-server"},
            ) as dl,
        ):
            out = pm._ensure_llama_server_binary(interactive=False, hardware="cpu")
        assert out["success"] is True
        assert out["binary_path"] == "/runtime/llama-server"
        dl.assert_called_once_with("cpu")
