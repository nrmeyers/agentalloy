"""Paired per-task comparator between two benchmark run directories (AC2.6).

Seeds are deterministic (``sha256(task_id:condition:run_index)``), so any two
runs of the same task set are exactly pairable per cell — the paired deltas
cancel task difficulty and seed variance out of the comparison.

    uv run python -m eval.compare_runs eval/runs/<baseline> eval/runs/<candidate>
    uv run python -m eval.compare_runs A B --condition composed

Prints per-task mean score deltas per condition (candidate - baseline), plus
condition totals with mean injected prompt size, and flags any cell pairs
whose seeds diverge (protocol drift — the runs are then NOT paired).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


def load_run(run_dir: Path) -> dict[tuple[str, str, int], dict[str, Any]]:
    """Map (task_id, condition, run_index) -> run meta for one run directory."""
    cells: dict[tuple[str, str, int], dict[str, Any]] = {}
    for meta_path in run_dir.glob("*/*/run-*.meta.json"):
        meta = json.loads(meta_path.read_text())
        key = (str(meta["task_id"]), str(meta["condition"]), int(meta["run_index"]))
        cells[key] = meta
    if not cells:
        raise FileNotFoundError(f"no run-*.meta.json under {run_dir}")
    return cells


def paired_deltas(
    baseline: dict[tuple[str, str, int], dict[str, Any]],
    candidate: dict[tuple[str, str, int], dict[str, Any]],
    condition: str | None = None,
) -> dict[str, Any]:
    """Per-task and total paired score deltas over the shared cells."""
    shared = sorted(set(baseline) & set(candidate))
    if condition is not None:
        shared = [k for k in shared if k[1] == condition]

    seed_mismatches: list[str] = []
    by_task_cond: dict[tuple[str, str], list[float]] = defaultdict(list)
    tokens: dict[str, dict[str, list[int]]] = defaultdict(lambda: {"base": [], "cand": []})
    for key in shared:
        b, c = baseline[key], candidate[key]
        if b.get("seed") != c.get("seed"):
            seed_mismatches.append(f"{key[0]}/{key[1]}/run-{key[2]}")
            continue
        by_task_cond[(key[0], key[1])].append(float(c["score"]) - float(b["score"]))
        if b.get("input_tokens") is not None and c.get("input_tokens") is not None:
            tokens[key[1]]["base"].append(int(b["input_tokens"]))
            tokens[key[1]]["cand"].append(int(c["input_tokens"]))

    per_task: dict[str, dict[str, float]] = defaultdict(dict)
    cond_deltas: dict[str, list[float]] = defaultdict(list)
    for (task_id, cond), deltas in sorted(by_task_cond.items()):
        mean = sum(deltas) / len(deltas)
        per_task[task_id][cond] = round(mean, 4)
        cond_deltas[cond].extend(deltas)

    totals = {
        cond: {
            "mean_delta": round(sum(d) / len(d), 4),
            "n_pairs": len(d),
            "mean_input_tokens_baseline": (
                round(sum(tokens[cond]["base"]) / len(tokens[cond]["base"]), 1)
                if tokens[cond]["base"]
                else None
            ),
            "mean_input_tokens_candidate": (
                round(sum(tokens[cond]["cand"]) / len(tokens[cond]["cand"]), 1)
                if tokens[cond]["cand"]
                else None
            ),
        }
        for cond, d in sorted(cond_deltas.items())
    }
    return {
        "paired_cells": len(shared) - len(seed_mismatches),
        "seed_mismatches": seed_mismatches,
        "per_task": dict(per_task),
        "totals": totals,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path, help="baseline run directory")
    parser.add_argument("candidate", type=Path, help="candidate run directory")
    parser.add_argument("--condition", default=None, help="restrict to one condition")
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = parser.parse_args(argv)

    report = paired_deltas(
        load_run(args.baseline), load_run(args.candidate), condition=args.condition
    )

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    print(f"paired cells: {report['paired_cells']}")
    if report["seed_mismatches"]:
        print(
            f"WARNING: {len(report['seed_mismatches'])} cells with diverging seeds "
            f"(excluded — runs are not fully paired):",
            file=sys.stderr,
        )
        for m in report["seed_mismatches"]:
            print(f"  {m}", file=sys.stderr)

    conds = sorted({c for t in report["per_task"].values() for c in t})
    header = f"{'task':42}" + "".join(f"{c:>20}" for c in conds)
    print("\nper-task mean score delta (candidate - baseline):")
    print(header)
    for task_id, per_cond in report["per_task"].items():
        row = f"{task_id:42}"
        for c in conds:
            v = per_cond.get(c)
            row += f"{v:>+20.3f}" if v is not None else f"{'—':>20}"
        print(row)

    print("\ntotals:")
    for cond, t in report["totals"].items():
        tok = ""
        if t["mean_input_tokens_baseline"] is not None:
            tok = (
                f"  input_tok {t['mean_input_tokens_baseline']} -> "
                f"{t['mean_input_tokens_candidate']}"
            )
        print(f"  {cond:20} delta {t['mean_delta']:+.4f}  (n={t['n_pairs']}){tok}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
