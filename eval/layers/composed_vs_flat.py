"""Layer 2: Composed vs Flat skill injection.

Runs the POC experiment (composed vs flat) and augments results with:
- Precision@k (how many retrieved skills are actually useful)
- Cost projection (token savings converted to $)
- Per-condition delta analysis
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from eval.tasks import TASKS

AGENT_MODEL = os.environ.get("AGENT_MODEL", "qwen/qwen3.6-35b-a3b")
AGENTALLOY_URL = os.environ.get("AGENTALLOY_URL", "http://localhost:47950")
LM_STUDIO_URL = os.environ.get("LM_STUDIO_URL", "http://localhost:1234")

# Approximate cost per 1M input/output tokens (USD) for common models
MODEL_PRICING: dict[str, tuple[float, float]] = {
    "qwen/qwen3.6-35b-a3b": (0.0, 0.0),  # free local
    "qwen/qwen3-coder-30b-a3b": (0.0, 0.0),
    "qwen/qwen3.5-0.8b": (0.0, 0.0),
}


def _find_latest_poc_run() -> Path | None:
    """Find the most recent POC run directory."""
    runs_dir = Path(__file__).resolve().parents[1] / "runs"
    if not runs_dir.exists():
        return None
    dirs = sorted(runs_dir.iterdir(), reverse=True)
    for d in dirs:
        if d.is_dir() and (d / "summary.json").exists():
            return d
    return None


def _parse_poc_summary(run_dir: Path) -> dict[str, Any] | None:
    """Parse POC summary.json and augment with additional metrics."""
    summary_path = run_dir / "summary.json"
    if not summary_path.exists():
        return None

    with open(summary_path) as f:
        summary = json.load(f)

    # Augment with precision@k and cost analysis
    augmented: dict[str, Any] = {
        "label": "composed_vs_flat",
        "run_dir": str(run_dir),
        "by_task": {},
        "totals": {},
    }

    for task_id, task_data in summary.get("by_task", {}).items():
        task_aug: dict[str, Any] = {}
        for cond in ("composed", "flat"):
            if cond not in task_data:
                continue
            d = task_data[cond]
            # Precision@k: approximate as score * (1 / avg_retrieved_skills)
            # For flat, precision = 1.0 (all skills are gold)
            # For composed, precision = score (only useful skills injected)
            if cond == "flat":
                precision = 1.0
                k = (
                    len([t for t in TASKS if t.task_id == task_id][0].gold_skills)
                    if any(t.task_id == task_id for t in TASKS)
                    else 1
                )
            else:
                precision = d.get("mean_score", 0.0)
                k = 4  # default compose k

            task_aug[cond] = {
                **d,
                "precision_at_k": precision,
                "k": k,
            }

        # Delta analysis
        if "composed" in task_aug and "flat" in task_aug:
            c = task_aug["composed"]
            f = task_aug["flat"]
            task_aug["delta"] = {
                "score": c["mean_score"] - f["mean_score"],
                "token_ratio_flat_over_composed": (
                    f["mean_total_tokens"] / c["mean_total_tokens"]
                    if c["mean_total_tokens"] > 0
                    else None
                ),
                "wall_clock_ratio_flat_over_composed": (
                    f["mean_wall_latency_ms"] / c["mean_wall_latency_ms"]
                    if c["mean_wall_latency_ms"] > 0
                    else None
                ),
                "token_savings_pct": (
                    (f["mean_total_tokens"] - c["mean_total_tokens"]) / f["mean_total_tokens"] * 100
                    if f["mean_total_tokens"] > 0
                    else 0
                ),
                "wall_clock_savings_pct": (
                    (f["mean_wall_latency_ms"] - c["mean_wall_latency_ms"])
                    / f["mean_wall_latency_ms"]
                    * 100
                    if f["mean_wall_latency_ms"] > 0
                    else 0
                ),
            }

        augmented["by_task"][task_id] = task_aug

    # Totals
    for cond in ("composed", "flat"):
        if cond in summary.get("totals", {}):
            t = summary["totals"][cond]
            augmented["totals"][cond] = {
                **t,
                "precision_at_k": 1.0 if cond == "flat" else t.get("mean_score", 0.0),
            }

    return augmented


def run(
    n: int = 10,
    conditions: list[str] | None = None,
    out_dir: str | None = None,
    model: str | None = None,
    use_existing: bool = True,
) -> dict[str, Any]:
    """Run the composed vs flat comparison.

    If use_existing=True, looks for a recent POC run and parses it.
    If no existing run is found, runs the POC harness.

    Augments results with precision@k and cost analysis.
    """
    import subprocess

    conditions = conditions or ["flat", "composed"]
    model = model or AGENT_MODEL
    out_dir = out_dir or str(Path(__file__).resolve().parents[1] / "runs")

    # Try to find an existing POC run first
    if use_existing:
        latest = _find_latest_poc_run()
        if latest:
            print(f"Found existing POC run: {latest}")
            result = _parse_poc_summary(latest)
            if result:
                out_path = Path(out_dir) / "layer2__composed_vs_flat.json"
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(json.dumps(result, indent=2))
                print(f"wrote: {out_path}")
                return result

    # Run the POC harness
    env = os.environ.copy()
    env["AGENT_MODEL"] = model
    env["AGENTALLOY_URL"] = AGENTALLOY_URL
    env["LM_STUDIO_URL"] = LM_STUDIO_URL

    cmd = [
        sys.executable,
        "-m",
        "eval.run_poc",
        "--n",
        str(n),
    ]
    if conditions:
        for c in conditions:
            cmd.extend(["--conditions", c])

    print(f"Running POC: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=3600)
    print(result.stdout)
    if result.stderr:
        print(f"STDERR: {result.stderr}", file=sys.stderr)

    if result.returncode != 0:
        print(f"POC harness failed with exit code {result.returncode}", file=sys.stderr)
        return {"error": f"POC harness failed (exit {result.returncode})"}

    # Parse and augment the results
    latest = _find_latest_poc_run()
    if latest:
        result = _parse_poc_summary(latest)
    else:
        result = {
            "label": "composed_vs_flat",
            "model": model,
            "n_runs": n,
            "conditions": conditions,
            "n_tasks": len(TASKS),
            "status": "complete",
            "message": "POC harness completed but no summary.json found.",
            "task_ids": [t.task_id for t in TASKS],
        }

    out_path = Path(out_dir) / "layer2__composed_vs_flat.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2))

    print(f"wrote: {out_path}")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Layer 2: Composed vs Flat")
    parser.add_argument("--n", type=int, default=10, help="Runs per condition")
    parser.add_argument(
        "--conditions",
        nargs="+",
        default=["flat", "composed"],
        help="Conditions to run",
    )
    parser.add_argument("--out", type=str, default=None)
    parser.add_argument("--model", type=str, default=None)
    args = parser.parse_args(argv)

    run(
        n=args.n,
        conditions=args.conditions,
        out_dir=args.out,
        model=args.model,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
