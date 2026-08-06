# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportPrivateUsage=false
"""``recommend-host-targets`` subcommand.

Given confirmed hardware JSON, return valid host targets
(dGPU / iGPU / CPU+RAM) with tradeoff notes and a single
``recommended: true`` flagged target.

Preference order: dGPU > iGPU > CPU+RAM.

Pyright suppression rationale: this module reads dynamically-shaped JSON
(hardware-detect output) and pyright's strict mode flags every nested
``.get()`` call as partially-unknown. The JSON contract is documented in
docs/install/contracts.md and validated at the integration boundary; using
TypedDicts here would not interoperate with dict literals passed by tests.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from agentalloy.install import state as install_state

SCHEMA_VERSION = 1

# Preference order — first available wins ``recommended: true``.
_PREFERENCE_ORDER = ["dGPU", "iGPU", "CPU+RAM"]


# ---------------------------------------------------------------------------
# Target evaluation
# ---------------------------------------------------------------------------


def _evaluate_dgpu(hw: dict[str, Any]) -> dict[str, Any]:
    gpu = hw.get("gpu") or {}
    discrete = gpu.get("discrete") or []
    available = len(discrete) > 0
    if available:
        models = ", ".join(d.get("model", "unknown") for d in discrete)
        vram = [d.get("vram_gb") for d in discrete if d.get("vram_gb")]
        vram_note = f" with {sum(vram)} GB VRAM" if vram else ""
        reason = f"{models}{vram_note}"
    else:
        reason = "No discrete GPU detected"
    return {
        "target": "dGPU",
        "available": available,
        "recommended": False,
        "reason": reason,
        "notes": None,
    }


def _evaluate_igpu(hw: dict[str, Any]) -> dict[str, Any]:
    gpu = hw.get("gpu") or {}
    integrated = gpu.get("integrated") or []
    metal = hw.get("metal", False)
    available = len(integrated) > 0 or metal
    if available:
        if integrated:
            models = ", ".join(d.get("model", "unknown") for d in integrated)
            vram = [d.get("vram_gb") for d in integrated if d.get("vram_gb")]
            vram_note = f" with {sum(vram)} GB shared VRAM" if vram else ""
            reason = f"{models}{vram_note}"
        else:
            reason = "Metal-capable GPU detected"
        notes = "Works for both embedding and chat. Shared with display compositor — may lag on heavy GPU load."
    else:
        reason = "No integrated GPU detected"
        notes = None
    return {
        "target": "iGPU",
        "available": available,
        "recommended": False,
        "reason": reason,
        "notes": notes,
    }


def _evaluate_cpu_ram(hw: dict[str, Any]) -> dict[str, Any]:
    mem = hw.get("memory_gb")
    reason = f"Always available; {mem} GB RAM provides headroom" if mem else "Always available"
    notes = "Slower than GPU; acceptable for runtime path (<200ms embed). Authoring will be noticeably slower."
    return {
        "target": "CPU+RAM",
        "available": True,
        "recommended": False,
        "reason": reason,
        "notes": notes,
    }


_EVALUATORS = {
    "dGPU": _evaluate_dgpu,
    "iGPU": _evaluate_igpu,
    "CPU+RAM": _evaluate_cpu_ram,
}


def recommend_targets(hw: dict[str, Any]) -> dict[str, Any]:
    """Evaluate host targets and return the contract-shaped result."""
    targets: list[dict[str, Any]] = []
    recommended_set = False

    for target_name in _PREFERENCE_ORDER:
        entry = _EVALUATORS[target_name](hw)
        if entry["available"] and not recommended_set:
            entry["recommended"] = True
            recommended_set = True
        targets.append(entry)

    return {"schema_version": SCHEMA_VERSION, "targets": targets}


# ---------------------------------------------------------------------------
# Subcommand interface
# ---------------------------------------------------------------------------


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p: argparse.ArgumentParser = subparsers.add_parser(
        "recommend-host-targets",
        help="Given confirmed hardware, return valid host targets with tradeoff notes.",
    )
    p.add_argument(
        "--hardware",
        required=True,
        help="Path to the detect output JSON file.",
    )
    p.set_defaults(func=run)


def _load_hardware(path_str: str) -> dict[str, Any]:
    """Load hardware JSON from a file path or from the state outputs dir."""
    p = Path(path_str)
    if not p.exists():
        print(f"ERROR: Hardware file not found: {path_str}", file=sys.stderr)
        print("CAUSE: The detect step may not have run yet.", file=sys.stderr)
        print("FIX:   Run `python -m agentalloy.install detect` first.", file=sys.stderr)
        raise SystemExit(1)
    return json.loads(p.read_text())


def run(args: argparse.Namespace) -> int:
    """Execute the recommend-host-targets subcommand.

    Always re-evaluates from the supplied --hardware file. The user may pass
    corrected hardware on a re-run; caching the previous result would silently
    ignore the correction.
    """
    st = install_state.load_state()
    hw = _load_hardware(args.hardware)
    result = recommend_targets(hw)

    fp, digest = install_state.save_output_file(result, "recommend-host-targets.json")
    install_state.record_step(
        st,
        "recommend-host-targets",
        extra={
            "output_digest": digest,
            "output_path": str(fp),
        },
    )
    install_state.save_state(st)

    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0
