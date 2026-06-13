"""Re-grade saved v3 domain run outputs with OLD vs NEW graders and report the delta.

The domain graders (``eval/domain_tasks.py``) score agent outputs by literal
substring matching. A 2026-06-13 robustness audit loosened a handful of
criteria that were under-crediting correct answers phrased differently (e.g.
the canonical OpenTelemetry field ``parent_span_id`` did not match a check
looking for ``parent_id``). This script measures the impact of those changes on
the headline benchmark numbers, WITHOUT mutating any stored ``run-N.meta.json``.

OLD score == the grades stored in each ``run-N.meta.json`` (verified to
reproduce the committed grader exactly, 1200/1200). NEW score == the current
(edited) ``DOMAIN_GRADERS`` re-run over each ``run-N.txt``. We report, per model
and per condition, the OLD vs NEW mean score, and the change in the headline
``composed - none`` lift plus the ``flat`` (oracle) ceiling.

Usage::

    uv run python -m eval.regrade_compare
    uv run python -m eval.regrade_compare --runs-root /abs/path/to/eval/runs
    uv run python -m eval.regrade_compare --markdown   # emit the PR table

By default it auto-discovers ``*__domain-*-v3`` directories under the runs root.
Because ``eval/runs/`` is gitignored, the run artifacts live in the primary
checkout; pass ``--runs-root`` when running from a worktree.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

from eval.domain_tasks import DOMAIN_GRADERS as _DOMAIN_GRADERS

Grader = Callable[[str], dict[str, bool]]

# DOMAIN_GRADERS is annotated dict[str, object] at its source; re-bind with the
# precise callable type so the graders type-check when invoked (mirrors the
# `# type: ignore[assignment]` idiom run_poc.py uses).
DOMAIN_GRADERS: dict[str, Grader] = _DOMAIN_GRADERS  # type: ignore[assignment]

MODELS = ("LFM", "12B", "27B", "35B")
CONDITIONS = ("none", "composed", "flat", "external")


def _model_of(run_dir_name: str) -> str | None:
    for m in MODELS:
        if f"domain-{m}-v3" in run_dir_name:
            return m
    return None


def _score(grades: dict[str, bool]) -> float:
    """Replicates run_poc scoring: fraction of criteria that are True."""
    return sum(1 for v in grades.values() if v) / len(grades) if grades else 0.0


def discover_v3_dirs(runs_root: Path) -> list[Path]:
    return sorted(p for p in runs_root.glob("*__domain-*-v3") if p.is_dir())


def collect(runs_root: Path) -> dict[tuple[str, str], dict[str, list[float]]]:
    """Return {(model, condition): {"old": [...scores], "new": [...scores]}}.

    Verifies along the way that the stored grades reproduce exactly under the
    *current* committed grader output recorded in meta.json (it cannot, since we
    have edited the grader — so we trust the stored grades as the OLD baseline,
    which was confirmed equal to the committed grader before editing).
    """
    cells: dict[tuple[str, str], dict[str, list[float]]] = defaultdict(
        lambda: {"old": [], "new": []}
    )
    for run_dir in discover_v3_dirs(runs_root):
        model = _model_of(run_dir.name)
        if model is None:
            continue
        for meta_path in run_dir.glob("*/*/run-*.meta.json"):
            meta = json.loads(meta_path.read_text())
            task_id = meta["task_id"]
            cond = meta["condition"]
            grader = DOMAIN_GRADERS.get(task_id)
            if grader is None:
                continue
            txt_path = meta_path.with_name(meta_path.name[: -len(".meta.json")] + ".txt")
            output = txt_path.read_text()

            old_grades: dict[str, bool] = meta.get("grades", {})
            old_score = meta.get("score", _score(old_grades))
            new_grades = grader(output)
            new_score = _score(new_grades)

            cells[(model, cond)]["old"].append(old_score)
            cells[(model, cond)]["new"].append(new_score)
    return cells


def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def criterion_flips(runs_root: Path) -> list[tuple[str, str, int]]:
    """Which (task, criterion) newly flip False->True, and how many times.

    Pure False->True is the only direction the loosening can produce; if any
    True->False appears it is a logic bug and is flagged.
    """
    flips: dict[tuple[str, str], int] = defaultdict(int)
    regressions: dict[tuple[str, str], int] = defaultdict(int)
    for run_dir in discover_v3_dirs(runs_root):
        if _model_of(run_dir.name) is None:
            continue
        for meta_path in run_dir.glob("*/*/run-*.meta.json"):
            meta = json.loads(meta_path.read_text())
            grader = DOMAIN_GRADERS.get(meta["task_id"])
            if grader is None:
                continue
            txt_path = meta_path.with_name(meta_path.name[: -len(".meta.json")] + ".txt")
            new_grades = grader(txt_path.read_text())
            old_grades = meta.get("grades", {})
            for crit, new_v in new_grades.items():
                old_v = old_grades.get(crit)
                if old_v is False and new_v is True:
                    flips[(meta["task_id"], crit)] += 1
                elif old_v is True and new_v is False:
                    regressions[(meta["task_id"], crit)] += 1
    out = [(t, c, n) for (t, c), n in sorted(flips.items(), key=lambda kv: -kv[1])]
    for (t, c), n in regressions.items():
        out.append((t, c, -n))  # negative count signals a regression to surface
    return out


def render(cells: dict[tuple[str, str], dict[str, list[float]]], markdown: bool) -> str:
    lines: list[str] = []
    present_conditions = [c for c in CONDITIONS if any((m, c) in cells for m in MODELS)]

    if markdown:
        header = "| model | condition | old mean | new mean | Δ | n |"
        sep = "|---|---|---:|---:|---:|---:|"
        lines += [header, sep]
    else:
        lines.append(f"{'model':6} {'cond':9} {'old':>6} {'new':>6} {'Δ':>7} {'n':>4}")

    for model in MODELS:
        if not any((model, c) in cells for c in present_conditions):
            continue
        for cond in present_conditions:
            cell = cells.get((model, cond))
            if cell is None:
                continue
            o, n = mean(cell["old"]), mean(cell["new"])
            cnt = len(cell["old"])
            if markdown:
                lines.append(f"| {model} | {cond} | {o:.3f} | {n:.3f} | {n - o:+.3f} | {cnt} |")
            else:
                lines.append(f"{model:6} {cond:9} {o:6.3f} {n:6.3f} {n - o:+7.3f} {cnt:4d}")

    # Headline: composed-none lift (old vs new) and flat ceiling per model.
    lines.append("")
    if markdown:
        lines.append("### Headline: composed−none lift and flat (oracle) ceiling")
        lines.append("")
        lines.append("| model | lift old | lift new | Δlift | flat old | flat new | Δflat |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|")
    else:
        lines.append("HEADLINE  composed-none lift  &  flat ceiling")
        lines.append(
            f"{'model':6} {'lift_old':>9} {'lift_new':>9} {'Δlift':>8} "
            f"{'flat_old':>9} {'flat_new':>9} {'Δflat':>8}"
        )
    for model in MODELS:
        none = cells.get((model, "none"))
        comp = cells.get((model, "composed"))
        flat = cells.get((model, "flat"))
        if not (none and comp):
            continue
        lift_old = mean(comp["old"]) - mean(none["old"])
        lift_new = mean(comp["new"]) - mean(none["new"])
        f_old = mean(flat["old"]) if flat else float("nan")
        f_new = mean(flat["new"]) if flat else float("nan")
        if markdown:
            lines.append(
                f"| {model} | {lift_old:+.3f} | {lift_new:+.3f} | "
                f"{lift_new - lift_old:+.3f} | {f_old:.3f} | {f_new:.3f} | "
                f"{f_new - f_old:+.3f} |"
            )
        else:
            lines.append(
                f"{model:6} {lift_old:+9.3f} {lift_new:+9.3f} "
                f"{lift_new - lift_old:+8.3f} {f_old:9.3f} {f_new:9.3f} "
                f"{f_new - f_old:+8.3f}"
            )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runs-root",
        type=Path,
        default=Path(__file__).resolve().parent / "runs",
        help="directory containing *__domain-*-v3 run dirs (default: eval/runs)",
    )
    parser.add_argument("--markdown", action="store_true", help="emit a markdown table")
    args = parser.parse_args()

    runs_root: Path = args.runs_root
    dirs = discover_v3_dirs(runs_root)
    if not dirs:
        raise SystemExit(
            f"no *__domain-*-v3 dirs under {runs_root} — pass --runs-root "
            f"(eval/runs is gitignored; artifacts live in the primary checkout)"
        )

    cells = collect(runs_root)
    print(render(cells, markdown=args.markdown))

    print("\nCriterion flips (False->True newly passing; negative = regression):")
    for task_id, crit, count in criterion_flips(runs_root):
        tag = "  REGRESSION" if count < 0 else ""
        print(f"  {abs(count):4d}  {task_id}/{crit}{tag}")


if __name__ == "__main__":
    main()
