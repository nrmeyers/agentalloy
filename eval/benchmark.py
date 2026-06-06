"""Benchmark orchestrator — runs all layers and produces a unified report.

Usage:
    uv run python -m eval.benchmark              # run all layers
    uv run python -m eval.benchmark --layer 1     # run layer 1 only
    uv run python -m eval.benchmark --layer 1 2   # run layers 1,2
    uv run python -m eval.benchmark --dry-run     # show what would run

Layers:
    1  Retrieval quality (recall@k, precision@k, MRR, contamination)
    2  Composed vs flat skill injection
    3  Cross-model robustness
    4  Composition idempotency
    5  Multi-phase session simulation
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LAYER_MODULES = {
    1: "eval.layers.retrieval_quality",
    2: "eval.layers.composed_vs_flat",
    3: "eval.layers.cross_model",
    4: "eval.layers.idempotency",
    5: "eval.layers.session_simulation",
}

LAYERS_DESC = {
    1: "Retrieval quality (recall@k, precision@k, MRR)",
    2: "Composed vs flat skill injection",
    3: "Cross-model robustness",
    4: "Composition idempotency",
    5: "Multi-phase session simulation",
}


def run_layer(layer_num: int, **kwargs: Any) -> dict[str, Any]:
    """Import and run a single layer module."""
    import importlib

    module_name = LAYER_MODULES[layer_num]
    module = importlib.import_module(module_name)
    return module.run(**kwargs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="AgentAlloy benchmark — run all layers or specific ones"
    )
    parser.add_argument(
        "--layer",
        nargs="+",
        type=int,
        default=None,
        help="Layer numbers to run (1-5). Default: all.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Show what would run")
    parser.add_argument("--out", type=str, default=None, help="Output directory")
    parser.add_argument("--k", type=int, default=4, help="Default k for retrieval layers")
    parser.add_argument("--n", type=int, default=10, help="Default runs per task")
    args = parser.parse_args(argv)

    layer_nums = args.layer or sorted(LAYER_MODULES.keys())

    # Validate layer numbers
    for ln in layer_nums:
        if ln not in LAYER_MODULES:
            print(f"ERROR: unknown layer {ln}. Valid: {sorted(LAYER_MODULES.keys())}")
            return 1

    if args.dry_run:
        print("=== Dry Run ===")
        for ln in layer_nums:
            print(f"  Layer {ln}: {LAYERS_DESC[ln]}")
        print()
        print("To run, omit --dry-run")
        return 0

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir = args.out or f"eval/runs/benchmark__{timestamp}"
    Path(out_dir).mkdir(parents=True, exist_ok=True)

    results: dict[str, Any] = {
        "timestamp": timestamp,
        "layers": {},
    }

    for ln in layer_nums:
        desc = LAYERS_DESC[ln]
        print(f"\n{'=' * 60}")
        print(f"Layer {ln}: {desc}")
        print(f"{'=' * 60}")

        # Pass layer-specific kwargs
        layer_kwargs = {"out_dir": out_dir, "k": args.k, "n": args.n}
        try:
            layer_result = run_layer(ln, **layer_kwargs)
            results["layers"][str(ln)] = layer_result
            print(f"Layer {ln}: OK")
        except Exception as e:
            print(f"Layer {ln}: FAILED — {e}", file=sys.stderr)
            results["layers"][str(ln)] = {"error": str(e)}
            continue

    # Write unified summary
    summary_path = Path(out_dir) / "summary.json"
    summary_path.write_text(json.dumps(results, indent=2))

    print(f"\n{'=' * 60}")
    print(f"All done. Summary: {summary_path}")
    print(f"{'=' * 60}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
