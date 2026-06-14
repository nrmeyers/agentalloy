"""Unit tests for the ``recommend-models`` subcommand."""

from __future__ import annotations

from typing import Any

from agentalloy.install.subcommands.recommend_models import (
    PRESET_RESOLUTION_TABLE,
    _classify_hardware,  # pyright: ignore[reportPrivateUsage]
    _resolve_preset,  # pyright: ignore[reportPrivateUsage]
    recommend_models,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _hw(
    *,
    os_kind: str = "linux",
    arch: str = "x86_64",
    cpu_vendor: str = "amd",
    discrete_gpus: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "os": {"kind": os_kind, "arch": arch},
        "cpu": {"vendor": cpu_vendor, "model": "Test"},
        "gpu": {"discrete": discrete_gpus or [], "integrated": []},
        "npu": {"present": False, "vendor": None, "model": None},
        "metal": False,
    }


# ---------------------------------------------------------------------------
# Hardware classification
# ---------------------------------------------------------------------------


class TestClassifyHardware:
    def test_apple_silicon(self) -> None:
        hw = _hw(os_kind="macos", arch="arm64")
        assert _classify_hardware(hw) == "apple-silicon"

    def test_nvidia_dgpu(self) -> None:
        hw = _hw(discrete_gpus=[{"vendor": "nvidia", "model": "RTX 4090", "vram_gb": 24}])
        assert _classify_hardware(hw) == "nvidia"

    def test_amd_x86(self) -> None:
        hw = _hw(cpu_vendor="amd", arch="x86_64")
        assert _classify_hardware(hw) == "amd-x86_64"

    def test_intel_x86_is_generic(self) -> None:
        hw = _hw(cpu_vendor="intel", arch="x86_64")
        assert _classify_hardware(hw) == "generic"

    def test_nvidia_takes_precedence_over_amd_cpu(self) -> None:
        hw = _hw(
            cpu_vendor="amd", discrete_gpus=[{"vendor": "nvidia", "model": "RTX", "vram_gb": 8}]
        )
        assert _classify_hardware(hw) == "nvidia"


# ---------------------------------------------------------------------------
# Preset resolution
# ---------------------------------------------------------------------------


class TestResolvePreset:
    def test_amd_dgpu_resolves_radeon(self) -> None:
        assert _resolve_preset("amd-x86_64", "dGPU") == "radeon"

    def test_amd_igpu_resolves_radeon(self) -> None:
        assert _resolve_preset("amd-x86_64", "iGPU") == "radeon"

    def test_apple_silicon_igpu(self) -> None:
        assert _resolve_preset("apple-silicon", "iGPU") == "apple-silicon"

    def test_nvidia_dgpu(self) -> None:
        assert _resolve_preset("nvidia", "dGPU") == "nvidia"

    def test_fallback_to_cpu(self) -> None:
        assert _resolve_preset("generic", "CPU+RAM") == "cpu"

    def test_unknown_combo_defaults_cpu(self) -> None:
        assert _resolve_preset("unknown", "unknown") == "cpu"


# ---------------------------------------------------------------------------
# Full recommend_models
# ---------------------------------------------------------------------------


class TestRecommendModels:
    def test_output_schema(self) -> None:
        result = recommend_models(_hw(), "CPU+RAM")
        assert result["schema_version"] == 1
        assert "host_target" in result
        assert "preset" in result
        assert "options" in result
        assert "preset_resolution_table" in result

    def test_at_least_one_option(self) -> None:
        result = recommend_models(_hw(), "CPU+RAM")
        assert len(result["options"]) >= 1

    def test_default_option_flagged(self) -> None:
        result = recommend_models(_hw(), "CPU+RAM")
        defaults = [o for o in result["options"] if o.get("default")]
        assert len(defaults) == 1

    def test_option_has_required_fields(self) -> None:
        result = recommend_models(_hw(), "CPU+RAM")
        opt = result["options"][0]
        for key in ("embed_model", "embed_runner"):
            assert key in opt

    def test_option_has_no_ingest_fields(self) -> None:
        result = recommend_models(_hw(), "CPU+RAM")
        opt = result["options"][0]
        assert "ingest_model" not in opt
        assert "ingest_runner" not in opt

    def test_all_presets_use_qwen3_embedding_gguf(self) -> None:
        for hw_fn, host in [
            (_hw(), "CPU+RAM"),
            (_hw(os_kind="macos", arch="arm64"), "iGPU"),
            (_hw(discrete_gpus=[{"vendor": "nvidia", "model": "RTX", "vram_gb": 8}]), "dGPU"),
            (
                _hw(
                    cpu_vendor="amd",
                    discrete_gpus=[{"vendor": "amd", "model": "RX 7900 XTX", "vram_gb": 24}],
                ),
                "dGPU",
            ),
        ]:
            result = recommend_models(hw_fn, host)
            opt = result["options"][0]
            # llama-server is the sole runner; the embed GGUF is uniform.
            assert opt["embed_model"] == "Qwen3-Embedding-0.6B-Q8_0.gguf"
            assert opt["embed_runner"] == "llama-server"

    def test_every_option_carries_reranker(self) -> None:
        result = recommend_models(_hw(), "CPU+RAM")
        opt = result["options"][0]
        assert opt["rerank_model"] == "Qwen3-Reranker-0.6B-Q8_0.gguf"
        assert opt["rerank_runner"] == "llama-server"

    def test_radeon_uses_llama_server_runner(self) -> None:
        hw = _hw(
            cpu_vendor="amd",
            discrete_gpus=[{"vendor": "amd", "model": "RX 7900 XTX", "vram_gb": 24}],
        )
        result = recommend_models(hw, "dGPU")
        assert result["preset"] == "radeon"
        assert result["options"][0]["embed_runner"] == "llama-server"

    def test_amd_igpu_resolves_radeon_with_llama_server(self) -> None:
        hw = _hw(cpu_vendor="amd")
        result = recommend_models(hw, "iGPU")
        assert result["preset"] == "radeon"
        assert result["options"][0]["embed_runner"] == "llama-server"

    def test_apple_silicon_preset(self) -> None:
        hw = _hw(os_kind="macos", arch="arm64")
        result = recommend_models(hw, "iGPU")
        assert result["preset"] == "apple-silicon"

    def test_amd_cpu_only_resolves_to_cpu_preset(self) -> None:
        hw = _hw(cpu_vendor="amd")
        result = recommend_models(hw, "CPU+RAM")
        assert result["preset"] == "cpu"

    def test_preset_resolution_table_in_output(self) -> None:
        result = recommend_models(_hw(), "CPU+RAM")
        assert result["preset_resolution_table"] == PRESET_RESOLUTION_TABLE
