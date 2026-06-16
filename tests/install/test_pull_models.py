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

        # "Already present" for llama-server now requires both the GGUF (presence
        # check) and the runner binary on PATH, so mock shutil.which too.
        with (
            patch.dict(_PRESENCE_CHECKS, {"llama-server": always_true}),
            patch(
                "agentalloy.install.subcommands.pull_models.shutil.which",
                return_value="/usr/bin/llama-server",
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
        """All attempts fail → 4 attempts, partial cleaned, error surfaced."""
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
