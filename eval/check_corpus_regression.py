"""Corpus-regression comparator.

Reads the most recent ``retrieval-audit-*.json`` (from ``eval.retrieval_audit``)
and ``gold-hit-*.json`` (from ``eval.gold_hit``) under ``eval/runs/``, compares
them against the committed baselines in ``eval/corpus_baselines.json``, and
exits non-zero on regression so the nightly CI job fails loudly.

Regression rules (any one fails the build):

* ``name`` probe hit_rate drops more than ``tolerance`` below baseline.
* ``topic`` probe hit_rate drops more than ``tolerance`` below baseline.
* stranded-skill count exceeds the baseline.
* ``gold_hit`` drops below the baseline.

Improvements (better than baseline) PASS and print a notice suggesting a
baseline bump. Pure stdlib — no service, no network, no third-party deps.

Usage::

    uv run python eval/check_corpus_regression.py [--runs-dir DIR] [--baselines FILE]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNS_DIR = REPO_ROOT / "eval" / "runs"
DEFAULT_BASELINES = REPO_ROOT / "eval" / "corpus_baselines.json"


class RegressionError(Exception):
    """Raised when a required input file is missing or malformed."""


def _latest(runs_dir: Path, prefix: str) -> Path:
    """Return the most recent ``<prefix>*.json`` in ``runs_dir`` (lexical = chronological,
    since timestamps are ISO ``YYYY-MM-DDTHH-MM-SSZ``)."""
    matches = sorted(runs_dir.glob(f"{prefix}*.json"))
    if not matches:
        raise RegressionError(
            f"no '{prefix}*.json' found in {runs_dir} — run the audit/probe step first"
        )
    return matches[-1]


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data: Any = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RegressionError(f"could not read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RegressionError(f"{path} is not a JSON object")
    return data


def compare(
    audit: dict[str, Any], gold: dict[str, Any], baselines: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """Compare measured metrics against baselines.

    Returns ``(failures, notices)``. ``failures`` non-empty => regression (exit 1).
    ``notices`` carry improvement suggestions (do not fail the build).
    """
    failures: list[str] = []
    notices: list[str] = []

    tol = float(baselines["tolerance"])

    by_probe = audit.get("by_probe_type", {})

    def _hit_rate(probe: str) -> float:
        block = by_probe.get(probe)
        if not isinstance(block, dict) or "hit_rate" not in block:
            raise RegressionError(f"audit missing by_probe_type.{probe}.hit_rate")
        return float(block["hit_rate"])

    # --- name / topic probe hit rates: fail if more than `tol` below baseline ---
    for probe, base_key in (("name", "name_probe_hit_rate"), ("topic", "topic_probe_hit_rate")):
        measured = _hit_rate(probe)
        base = float(baselines[base_key])
        if measured < base - tol:
            failures.append(
                f"{probe} probe hit_rate REGRESSED: {measured:.4f} < "
                f"baseline {base:.4f} - tolerance {tol:.4f} ({base - tol:.4f})"
            )
        elif measured > base + tol:
            notices.append(
                f"{probe} probe hit_rate IMPROVED: {measured:.4f} > baseline {base:.4f} "
                f"(consider bumping '{base_key}')"
            )

    # --- per-phase hit-rate floors (optional baseline key) ---
    # Phase-level regressions (e.g. a k-default change depressing one phase's
    # hit@k) can hide inside a healthy overall probe rate, so each listed phase
    # gets its own floor. Generic over whatever phases the audit emits.
    phase_floors = baselines.get("phase_hit_rate_floors")
    if isinstance(phase_floors, dict):
        by_phase = audit.get("by_phase", {})
        for phase, floor in phase_floors.items():
            block = by_phase.get(phase)
            if not isinstance(block, dict) or "hit_rate" not in block:
                failures.append(
                    f"phase '{phase}' has a baseline floor but no audit measurement — "
                    f"did the audit stop probing it?"
                )
                continue
            measured = float(block["hit_rate"])
            base = float(floor)
            if measured < base - tol:
                failures.append(
                    f"phase '{phase}' hit_rate REGRESSED: {measured:.4f} < "
                    f"floor {base:.4f} - tolerance {tol:.4f}"
                )
            elif measured > base + tol:
                notices.append(
                    f"phase '{phase}' hit_rate IMPROVED: {measured:.4f} > floor {base:.4f} "
                    f"(consider bumping phase_hit_rate_floors.{phase})"
                )

    # --- stranded skills: fail if count exceeds baseline ---
    stranded = audit.get("stranded_skills")
    if not isinstance(stranded, list):
        raise RegressionError("audit missing 'stranded_skills' list")
    stranded_count = len(stranded)
    base_stranded = int(baselines["stranded_count"])
    if stranded_count > base_stranded:
        failures.append(
            f"stranded skill count REGRESSED: {stranded_count} > baseline {base_stranded} "
            f"(currently stranded: {stranded})"
        )
    elif stranded_count < base_stranded:
        notices.append(
            f"stranded skill count IMPROVED: {stranded_count} < baseline {base_stranded} "
            "(consider lowering 'stranded_count')"
        )

    # --- gold hit: fail if below baseline ---
    if "gold_hit" not in gold:
        raise RegressionError("gold-hit file missing 'gold_hit'")
    measured_gold = int(gold["gold_hit"])
    base_gold = int(baselines["gold_hit"])
    if measured_gold < base_gold:
        total = gold.get("gold_hit_total", baselines.get("gold_hit_total"))
        failures.append(f"gold_hit REGRESSED: {measured_gold}/{total} < baseline {base_gold}")
    elif measured_gold > base_gold:
        notices.append(
            f"gold_hit IMPROVED: {measured_gold} > baseline {base_gold} "
            "(consider bumping 'gold_hit')"
        )

    return failures, notices


def run(runs_dir: Path, baselines_path: Path) -> int:
    """Load latest run files + baselines, compare, print, and return an exit code."""
    baselines = _load_json(baselines_path)
    audit = _load_json(_latest(runs_dir, "retrieval-audit-"))
    gold = _load_json(_latest(runs_dir, "gold-hit-"))

    failures, notices = compare(audit, gold, baselines)

    for notice in notices:
        print(f"NOTICE: {notice}")

    if failures:
        print("\nCORPUS REGRESSION DETECTED:")
        for f in failures:
            print(f"  FAIL: {f}")
        return 1

    print("\nPASS: corpus metrics within tolerance of baselines.")
    if notices:
        print("(improvements above — consider updating eval/corpus_baselines.json)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python eval/check_corpus_regression.py")
    parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)
    parser.add_argument("--baselines", type=Path, default=DEFAULT_BASELINES)
    args = parser.parse_args(argv)
    try:
        return run(args.runs_dir, args.baselines)
    except RegressionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
