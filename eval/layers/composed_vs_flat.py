"""Layer 2: Composed vs MCP skill dictionary.

Runs the POC experiment with three arms:
  - composed: AgentAlloy JIT composition (targeted, phase-aware)
  - mcp: MCP skill dictionary (agent must decide which skills to fetch)
  - no_inject: Baseline, no injection at all

Augments results with precision@k, token savings, and delta analysis.

Key hypothesis: targeted JIT composition (composed) beats the MCP pattern
because the agent doesn't have to figure out which skills to fetch. The MCP
arm tests whether the agent can introspect and identify relevant skills —
a burden AgentAlloy removes entirely.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from eval.tasks import TASKS

AGENT_MODEL = os.environ.get("AGENT_MODEL", "qwen/qwen2.5-coder-14b")
AGENTALLOY_URL = os.environ.get("AGENTALLOY_URL", "http://localhost:47950")
LM_STUDIO_URL = os.environ.get("LM_STUDIO_URL", "http://localhost:1234")


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
        "label": "composed_vs_mcp",
        "run_dir": str(run_dir),
        "by_task": {},
        "totals": {},
    }

    for task_id, task_data in summary.get("by_task", {}).items():
        task_aug: dict[str, Any] = {}
        for cond in ("composed", "mcp", "no_inject"):
            if cond not in task_data:
                continue
            d = task_data[cond]
            # Precision@k: for composed, approximate as mean_score
            # (higher score means more relevant skills were injected)
            # For MCP, precision = mean_score (agent fetched skills)
            # For no_inject, precision = 0 (no skills)
            if cond == "no_inject":
                precision = 0.0
                k = 0
            else:
                precision = d.get("mean_score", 0.0)
                k = (
                    4
                    if cond == "composed"
                    else len([t for t in TASKS if t.task_id == task_id][0].gold_skills)
                    if any(t.task_id == task_id for t in TASKS)
                    else 1
                )

            task_aug[cond] = {
                **d,
                "precision_at_k": precision,
                "k": k,
            }

        # Delta analysis: composed vs mcp
        if "composed" in task_aug and "mcp" in task_aug:
            c = task_aug["composed"]
            m = task_aug["mcp"]
            task_aug["delta_composed_minus_mcp"] = {
                "score": c["mean_score"] - m["mean_score"],
                "token_savings_pct": (
                    (m["mean_total_tokens"] - c["mean_total_tokens"]) / m["mean_total_tokens"] * 100
                    if m["mean_total_tokens"] > 0
                    else 0
                ),
                "wall_clock_savings_pct": (
                    (m["mean_wall_latency_ms"] - c["mean_wall_latency_ms"])
                    / m["mean_wall_latency_ms"]
                    * 100
                    if m["mean_wall_latency_ms"] > 0
                    else 0
                ),
                "token_ratio_mcp_over_composed": (
                    m["mean_total_tokens"] / c["mean_total_tokens"]
                    if c["mean_total_tokens"] > 0
                    else None
                ),
            }

        # Delta analysis: composed vs no_inject
        if "composed" in task_aug and "no_inject" in task_aug:
            c = task_aug["composed"]
            n = task_aug["no_inject"]
            task_aug["delta_composed_minus_no_inject"] = {
                "score": c["mean_score"] - n["mean_score"],
            }

        augmented["by_task"][task_id] = task_aug

    # Totals
    for cond in ("composed", "mcp", "no_inject"):
        if cond in summary.get("totals", {}):
            t = summary["totals"][cond]
            augmented["totals"][cond] = {
                **t,
                "precision_at_k": 0.0 if cond == "no_inject" else t.get("mean_score", 0.0),
            }

    return augmented


def run(
    n: int = 10,
    conditions: list[str] | None = None,
    out_dir: str | None = None,
    model: str | None = None,
    use_existing: bool = True,
) -> dict[str, Any]:
    """Run the composed vs MCP comparison.

    If use_existing=True, looks for a recent POC run and parses it.
    If no existing run is found, runs the POC harness.
    """
    import subprocess

    conditions = conditions or ["composed", "mcp", "no_inject"]
    model = model or AGENT_MODEL
    out_dir = out_dir or str(Path(__file__).resolve().parents[1] / "runs")

    # Try to find an existing POC run first
    if use_existing:
        latest = _find_latest_poc_run()
        if latest:
            print(f"Found existing POC run: {latest}")
            result = _parse_poc_summary(latest)
            if result:
                out_path = Path(out_dir) / "layer2__composed_vs_mcp.json"
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
            "label": "composed_vs_mcp",
            "model": model,
            "n_runs": n,
            "conditions": conditions,
            "n_tasks": len(TASKS),
            "status": "complete",
            "message": "POC harness completed but no summary.json found.",
            "task_ids": [t.task_id for t in TASKS],
        }

    out_path = Path(out_dir) / "layer2__composed_vs_mcp.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2))

    print(f"wrote: {out_path}")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Layer 2: Composed vs MCP")
    parser.add_argument("--n", type=int, default=10, help="Runs per condition")
    parser.add_argument(
        "--conditions",
        nargs="+",
        default=["composed", "mcp", "no_inject"],
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
