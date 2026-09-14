"""Pure-retrieval recall@k harness — no agent model needed.

For each task in eval.tasks.TASKS, calls /compose and checks how many
gold_skills appear in the returned source_skills. Reports per-task and
aggregate recall@k. Used to A/B test embedding-side changes without
agent-side variance.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

from eval.tasks import TASKS

AGENTALLOY_URL = os.environ.get("AGENTALLOY_URL", "http://localhost:48950")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--k", type=int, default=4)
    parser.add_argument("--label", type=str, default="recall")
    parser.add_argument("--out", type=str, default=None)
    args = parser.parse_args(argv)

    rows: list[dict] = []
    total_gold = 0
    total_hits = 0
    full_recall_count = 0

    with httpx.Client(timeout=30.0) as client:
        for task in TASKS:
            resp = client.post(
                f"{AGENTALLOY_URL}/compose",
                json={"task": task.spec, "phase": task.phase, "k": args.k},
            )
            resp.raise_for_status()
            body = resp.json()
            source = body.get("source_skills", []) or []
            gold = list(task.gold_skills)
            hits = sum(1 for g in gold if g in source)
            recall = hits / len(gold) if gold else 0.0
            full = recall == 1.0
            full_recall_count += int(full)
            total_gold += len(gold)
            total_hits += hits
            rows.append(
                {
                    "task_id": task.task_id,
                    "phase": task.phase,
                    "gold": gold,
                    "retrieved": source,
                    "hits": hits,
                    "recall": recall,
                    "full_recall": full,
                    "compose_ms": body.get("compose_ms"),
                }
            )
            print(
                f"{task.task_id:35s} recall={recall:.2f} ({hits}/{len(gold)})  retrieved={source}"
            )

    micro = total_hits / total_gold if total_gold else 0.0
    macro = sum(r["recall"] for r in rows) / len(rows) if rows else 0.0
    summary = {
        "label": args.label,
        "k": args.k,
        "n_tasks": len(rows),
        "total_gold": total_gold,
        "total_hits": total_hits,
        "micro_recall": micro,
        "macro_recall": macro,
        "full_recall_count": full_recall_count,
        "per_task": rows,
    }

    out_path = Path(args.out or f"eval/runs/recall__{args.label}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, indent=2))

    print()
    print(f"=== {args.label} | k={args.k} | n={len(rows)} tasks ===")
    print(f"micro recall = {micro:.3f}  ({total_hits}/{total_gold} gold skills retrieved)")
    print(f"macro recall = {macro:.3f}  (avg per-task)")
    print(f"full-recall tasks: {full_recall_count}/{len(rows)}")
    print(f"wrote: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
