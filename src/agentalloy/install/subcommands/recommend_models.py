# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
"""``recommend-models`` subcommand.

Given hardware + chosen host target, return the ``{embed_model, embed_runner,
rerank_model, rerank_runner}`` option and the resolved preset name.

Preset resolution table (from contracts.md):
  (apple-silicon, iGPU)    → apple-silicon
  (nvidia, dGPU)           → nvidia
  (amd-x86_64, dGPU)       → radeon
  (amd-x86_64, iGPU)       → radeon
  (any, CPU+RAM)           → cpu

llama-server (llama.cpp) is the sole inference runner. There is no runner
choice: every preset serves the Qwen3 embed + reranker GGUFs through two
dedicated llama-server instances (embed on 47951, reranker on 47952). The
hardware difference is handled at server start via ``-ngl``, not by the
preset name.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from agentalloy.install import state as install_state
from agentalloy.install.output import add_json_flag, print_rich, write_result

SCHEMA_VERSION = 1

# The sole inference runner.
RUNNER = "llama-server"

# Shared model set served by every preset (both GGUFs, both via llama-server).
EMBED_MODEL = "nomic-embed-text-v1.5.Q8_0.gguf"
RERANK_MODEL = "Qwen3-Reranker-0.6B-Q8_0.gguf"

# ---- Preset resolution ---------------------------------------------------

_PRESET_TABLE: list[tuple[str, str, str]] = [
    # (hardware_class, host_target, preset)
    ("apple-silicon", "iGPU", "apple-silicon"),
    ("nvidia", "dGPU", "nvidia"),
    ("amd-x86_64", "dGPU", "radeon"),
    ("amd-x86_64", "iGPU", "radeon"),
]
_DEFAULT_PRESET = "cpu"

# Full resolution table exposed in output
PRESET_RESOLUTION_TABLE: dict[str, str] = {
    "(apple-silicon, iGPU)": "apple-silicon",
    "(nvidia, dGPU)": "nvidia",
    "(amd-x86_64, dGPU)": "radeon",
    "(amd-x86_64, iGPU)": "radeon",
    "(any, CPU+RAM)": "cpu",
}


# ---- Model options -------------------------------------------------------
# A single llama-server option carrying both the embed and reranker GGUFs.
# Per-hardware acceleration hints help the wizard surface the right copy.

_ACCEL_HINTS: dict[str, str] = {
    "apple-silicon": "Metal acceleration",
    "nvidia": "CUDA acceleration",
    "radeon": "Vulkan/ROCm acceleration",
    "cpu": "CPU-only",
}


def _options_for_preset(preset: str) -> list[dict[str, Any]]:
    accel = _ACCEL_HINTS.get(preset, "CPU-only")
    return [
        {
            "default": True,
            "embed_model": EMBED_MODEL,
            "embed_runner": RUNNER,
            "rerank_model": RERANK_MODEL,
            "rerank_runner": RUNNER,
            "embed_runner_install_hint": (
                f"llama-server (llama.cpp) with {accel}; the embed and reranker "
                "GGUFs will be downloaded from Hugging Face automatically."
            ),
        },
    ]


# ---- Hardware classification ---------------------------------------------


def _classify_hardware(hw: dict[str, Any]) -> str:
    """Return a hardware class string for preset resolution."""
    os_info = hw.get("os") or {}
    arch = os_info.get("arch", "")
    cpu = hw.get("cpu") or {}
    vendor = (cpu.get("vendor") or "").lower()
    gpu = hw.get("gpu") or {}
    discrete = gpu.get("discrete") or []

    # Apple Silicon
    if arch == "arm64" and os_info.get("kind") == "macos":
        return "apple-silicon"

    # NVIDIA dGPU present
    if any((d.get("vendor") or "").lower() == "nvidia" for d in discrete):
        return "nvidia"

    # AMD x86_64 — resolves to radeon (dGPU or iGPU) or cpu (CPU+RAM)
    if vendor == "amd" and "x86" in arch:
        return "amd-x86_64"

    return "generic"


def _resolve_preset(hw_class: str, host_target: str) -> str:
    """Resolve the preset name from hardware class and host target."""
    for cls, tgt, preset in _PRESET_TABLE:
        if cls == hw_class and tgt == host_target:
            return preset
    return _DEFAULT_PRESET


# ---- Public API ----------------------------------------------------------


def recommend_models(
    hw: dict[str, Any],
    host_target: str,
    runner: str | None = None,  # noqa: ARG001 — accepted for call-site compat; ignored
    interactive: bool | None = None,  # noqa: ARG001 — no runner choice to prompt for
) -> dict[str, Any]:
    """Evaluate the model option for the given hardware and host target.

    The ``runner`` and ``interactive`` parameters are retained for call-site
    compatibility but ignored: llama-server is the sole runner, so there is
    no runner choice to prompt for or override.
    """
    hw_class = _classify_hardware(hw)
    preset = _resolve_preset(hw_class, host_target)
    options = _options_for_preset(preset)
    selected_opt = options[0]

    return {
        "schema_version": SCHEMA_VERSION,
        "host_target": host_target,
        "preset": preset,
        "base_preset": preset,
        "selected_runner": selected_opt["embed_runner"],
        "options": options,
        "preset_resolution_table": PRESET_RESOLUTION_TABLE,
    }


# ---------------------------------------------------------------------------
# Subcommand interface
# ---------------------------------------------------------------------------


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    p: argparse.ArgumentParser = subparsers.add_parser(
        "recommend-models",
        help="Given hardware + host target, return the model set and resolved preset.",
    )
    p.add_argument(
        "--hardware",
        required=True,
        help="Path to the detect output JSON file.",
    )
    p.add_argument(
        "--host",
        required=True,
        choices=["dGPU", "iGPU", "CPU+RAM"],
        help="The chosen host target from recommend-host-targets.",
    )
    add_json_flag(p)
    p.set_defaults(func=run)


def _load_hardware(path_str: str) -> dict[str, Any]:
    p = Path(path_str)
    if not p.exists():
        print(f"ERROR: Hardware file not found: {path_str}", file=sys.stderr)
        print("CAUSE: The detect step may not have run yet.", file=sys.stderr)
        print("FIX:   Run `python -m agentalloy.install detect` first.", file=sys.stderr)
        raise SystemExit(1)
    return json.loads(p.read_text())


def _render_human(result: dict[str, Any]) -> None:
    """Render model recommendations in human-readable format."""
    preset = result.get("preset", "unknown")
    options = result.get("options", [])

    print_rich("\n  [bold]Model Recommendations[/bold]\n")
    print_rich(f"  Preset: [bold]{preset}[/bold]\n")

    for opt in options:
        default_marker = " [green](default)[/green]" if opt.get("default") else ""
        runner = opt.get("embed_runner", "?")
        embed = opt.get("embed_model", "?")
        rerank = opt.get("rerank_model", "?")
        print_rich(f"  {runner}: {embed}{default_marker}")
        print_rich(f"  {runner}: {rerank} (reranker)")

    print_rich()


def run(args: argparse.Namespace) -> int:
    """Execute the recommend-models subcommand."""
    st = install_state.load_state()
    hw = _load_hardware(args.hardware)
    result = recommend_models(hw, args.host)

    fp, digest = install_state.save_output_file(result, "recommend-models.json")

    selected = {}
    for opt in result["options"]:
        if opt.get("default"):
            selected = {
                "preset": result["preset"],
                "embed_model": opt["embed_model"],
                "embed_runner": opt["embed_runner"],
                "rerank_model": opt.get("rerank_model"),
                "rerank_runner": opt.get("rerank_runner"),
            }
            break

    install_state.record_step(
        st,
        "recommend-models",
        extra={
            "output_digest": digest,
            "output_path": str(fp),
            "selected": selected,
        },
    )
    install_state.save_state(st)

    write_result(result, args, human_fn=_render_human)
    return 0
