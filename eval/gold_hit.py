"""Domain gold-hit probe.

For each task in ``eval.domain_tasks.DOMAIN_TASKS``, POST the task spec to the
live ``/compose`` endpoint and check whether any of the task's gold skills
appears in the response's ``source_skills``. This is the realistic end-to-end
counterpart to ``retrieval_audit`` (which uses mechanical name/topic probes):
it asks "for a natural task phrasing, does the right skill actually surface?".

Prints per-task HIT/MISS lines and a ``gold-hit: N/M`` summary, writes a JSON
results file to ``eval/runs/gold-hit-<ts>.json``, and always exits 0 — the
regression comparator (``eval.check_corpus_regression``) judges the numbers.

Requires the AgentAlloy service running on ``$AGENTALLOY_URL`` (default
``http://localhost:47950``). Read-only; makes no model calls.

Usage::

    uv run python -m eval.gold_hit [--k 4]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from eval.domain_tasks import DOMAIN_TASKS

AGENTALLOY_URL = os.environ.get("AGENTALLOY_URL", "http://localhost:47950")
REPO_ROOT = Path(__file__).resolve().parents[1]
RUNS_ROOT = REPO_ROOT / "eval" / "runs"


def run_probe(k: int) -> dict[str, Any]:
    """Run the gold-hit probe across all domain tasks. Returns the report dict."""
    results: list[dict[str, Any]] = []
    hits = 0
    timeout = httpx.Timeout(connect=5.0, read=600.0, write=10.0, pool=5.0)
    with httpx.Client(timeout=timeout) as cli:
        for task in DOMAIN_TASKS:
            resp = cli.post(
                f"{AGENTALLOY_URL}/compose",
                json={"task": task.spec, "phase": task.phase, "k": k},
            )
            resp.raise_for_status()
            source_skills = resp.json().get("source_skills", []) or []
            gold = list(task.gold_skills)
            hit = any(g in source_skills for g in gold)
            if hit:
                hits += 1
            results.append(
                {
                    "task_id": task.task_id,
                    "phase": task.phase,
                    "gold_skills": gold,
                    "source_skills": source_skills,
                    "hit": hit,
                }
            )
            print(f"  {'HIT ' if hit else 'MISS'}  {task.task_id}  gold={gold}")

    total = len(DOMAIN_TASKS)
    print(f"\ngold-hit: {hits}/{total}")
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "k": k,
        "gold_hit": hits,
        "gold_hit_total": total,
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eval.gold_hit")
    parser.add_argument(
        "--k", type=int, default=4, help="Number of skills to request from /compose"
    )
    args = parser.parse_args(argv)

    report = run_probe(args.k)

    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    out_path = RUNS_ROOT / f"gold-hit-{stamp}.json"
    out_path.write_text(json.dumps(report, indent=2))
    print(f"\nwrote: {out_path}")
    return 0  # comparison step judges; the probe itself never fails the build


if __name__ == "__main__":
    sys.exit(main())
