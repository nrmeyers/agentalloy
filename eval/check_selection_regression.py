"""Judge a skill-selection report against ``eval/selection_baselines.json``.

Fails (exit 1) when the overall or phase-matched hit count drops below its
floor, or when more compose runs ended in an interpreter error than allowed.
Also lists per-task flips against the baseline run (informational — the
interpreter decodes greedily, but a borderline task can still flip). The
baseline was measured on the dev server's GPU sidecar — run it there
(``scripts/pre-pr-eval.sh``); CPU runs select differently and far slower.

Usage::

    uv run python -m eval.check_selection_regression [report.json]

With no argument, the newest ``eval/runs/skill-selection-*.json`` is judged.
Exit codes: 0 = within floors, 1 = regression, 2 = no/invalid report.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINES = REPO_ROOT / "eval" / "selection_baselines.json"
RUNS_ROOT = REPO_ROOT / "eval" / "runs"


def judge(report: dict[str, Any], baselines: dict[str, Any]) -> list[str]:
    floors = baselines["floors"]
    failures: list[str] = []
    if report["hits"] < floors["hits"]:
        failures.append(f"hits {report['hits']}/{report['total']} < floor {floors['hits']}")
    if report["matched_hits"] < floors["matched_hits"]:
        failures.append(
            f"phase-matched hits {report['matched_hits']}/{report['matched_total']} "
            f"< floor {floors['matched_hits']}"
        )
    if report["errors"] > baselines["max_errors"]:
        failures.append(f"{report['errors']} interpreter errors > max {baselines['max_errors']}")
    return failures


def flips(report: dict[str, Any], baselines: dict[str, Any]) -> tuple[list[str], list[str]]:
    expected = set(baselines["measured"]["hit_tasks"])
    got = {r["task_id"] for r in report["results"] if r["hit"]}
    return sorted(expected - got), sorted(got - expected)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args:
        path = Path(args[0])
    else:
        runs = sorted(RUNS_ROOT.glob("skill-selection-*.json"))
        if not runs:
            print("check-selection: no skill-selection report found", file=sys.stderr)
            return 2
        path = runs[-1]
    try:
        report = json.loads(path.read_text())
        baselines = json.loads(BASELINES.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"check-selection: cannot read report/baselines: {exc}", file=sys.stderr)
        return 2

    lost, gained = flips(report, baselines)
    print(
        f"skill-selection: {report['hits']}/{report['total']} "
        f"(phase-matched {report['matched_hits']}/{report['matched_total']}), "
        f"{report['errors']} errors — baseline {baselines['measured']['hits']}/"
        f"{baselines['measured']['total']}, floors {baselines['floors']}"
    )
    if lost:
        print(f"  lost vs baseline:   {', '.join(lost)}")
    if gained:
        print(f"  gained vs baseline: {', '.join(gained)}")

    failures = judge(report, baselines)
    if failures:
        print("SKILL-SELECTION REGRESSION:")
        for f in failures:
            print(f"  FAIL: {f}")
        return 1
    print("skill-selection: within floors")
    return 0


if __name__ == "__main__":
    sys.exit(main())
