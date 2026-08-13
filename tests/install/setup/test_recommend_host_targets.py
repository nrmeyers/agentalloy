"""Unit tests for the ``recommend-host-targets`` subcommand."""

from __future__ import annotations

from typing import Any

from agentalloy.install.subcommands.recommend_host_targets import (
    recommend_targets,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _hw(
    *,
    discrete_gpus: list[dict[str, Any]] | None = None,
    integrated_gpus: list[dict[str, Any]] | None = None,
    metal: bool = False,
    memory_gb: int | None = 64,
) -> dict[str, Any]:
    """Build a minimal detect-shaped hardware dict."""
    return {
        "os": {"kind": "linux", "arch": "x86_64"},
        "cpu": {"vendor": "amd", "model": "Test CPU"},
        "memory_gb": memory_gb,
        "gpu": {
            "discrete": discrete_gpus or [],
            "integrated": integrated_gpus or [],
        },
        "metal": metal,
    }


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


class TestRecommendTargetsSchema:
    def test_output_has_required_keys(self) -> None:
        result = recommend_targets(_hw())
        assert "schema_version" in result
        assert "targets" in result
        assert result["schema_version"] == 1

    def test_always_three_targets(self) -> None:
        result = recommend_targets(_hw())
        assert len(result["targets"]) == 3

    def test_each_target_has_required_fields(self) -> None:
        result = recommend_targets(_hw())
        for t in result["targets"]:
            assert "target" in t
            assert "available" in t
            assert "recommended" in t
            assert "reason" in t
            assert "notes" in t

    def test_target_names(self) -> None:
        result = recommend_targets(_hw())
        names = [t["target"] for t in result["targets"]]
        assert names == ["dGPU", "iGPU", "CPU+RAM"]


# ---------------------------------------------------------------------------
# Recommendation logic
# ---------------------------------------------------------------------------


class TestPreferenceOrder:
    def test_dgpu_recommended_when_present(self) -> None:
        result = recommend_targets(
            _hw(discrete_gpus=[{"vendor": "nvidia", "model": "RTX 4090", "vram_gb": 24}])
        )
        recommended = [t for t in result["targets"] if t["recommended"]]
        assert len(recommended) == 1
        assert recommended[0]["target"] == "dGPU"

    def test_igpu_recommended_when_no_dgpu(self) -> None:
        result = recommend_targets(
            _hw(integrated_gpus=[{"vendor": "intel", "model": "UHD 770", "vram_gb": None}])
        )
        recommended = [t for t in result["targets"] if t["recommended"]]
        assert len(recommended) == 1
        assert recommended[0]["target"] == "iGPU"

    def test_cpu_ram_recommended_as_fallback(self) -> None:
        result = recommend_targets(_hw())
        recommended = [t for t in result["targets"] if t["recommended"]]
        assert len(recommended) == 1
        assert recommended[0]["target"] == "CPU+RAM"

    def test_exactly_one_recommended(self) -> None:
        hw = _hw(
            discrete_gpus=[{"vendor": "nvidia", "model": "RTX", "vram_gb": 8}],
            integrated_gpus=[{"vendor": "amd", "model": "Radeon", "vram_gb": 4}],
        )
        result = recommend_targets(hw)
        recommended = [t for t in result["targets"] if t["recommended"]]
        assert len(recommended) == 1

    def test_cpu_ram_always_available(self) -> None:
        result = recommend_targets(_hw())
        cpu = next(t for t in result["targets"] if t["target"] == "CPU+RAM")
        assert cpu["available"] is True


class TestAvailability:
    def test_dgpu_not_available_when_empty(self) -> None:
        result = recommend_targets(_hw())
        dgpu = next(t for t in result["targets"] if t["target"] == "dGPU")
        assert dgpu["available"] is False

    def test_igpu_available_via_metal(self) -> None:
        result = recommend_targets(_hw(metal=True))
        igpu = next(t for t in result["targets"] if t["target"] == "iGPU")
        assert igpu["available"] is True

    def test_dgpu_vram_in_reason(self) -> None:
        result = recommend_targets(
            _hw(discrete_gpus=[{"vendor": "nvidia", "model": "RTX 4090", "vram_gb": 24}])
        )
        dgpu = next(t for t in result["targets"] if t["target"] == "dGPU")
        assert "24" in dgpu["reason"]

    def test_memory_in_cpu_reason(self) -> None:
        result = recommend_targets(_hw(memory_gb=128))
        cpu = next(t for t in result["targets"] if t["target"] == "CPU+RAM")
        assert "128" in cpu["reason"]
