"""Tests for the corpus-regression comparator (eval/check_corpus_regression.py).

Covers the comparator logic only — never touches a live service. Run files are
synthesized in a tmp path; the comparator reads the latest of each prefix.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from eval.check_corpus_regression import RegressionError, compare, run

BASELINES = {
    "name_probe_hit_rate": 0.901,
    "topic_probe_hit_rate": 0.921,
    "stranded_count": 18,
    "gold_hit": 7,
    "gold_hit_total": 8,
    "tolerance": 0.02,
}


def _audit(name: float, topic: float, stranded: int) -> dict[str, object]:
    return {
        "by_probe_type": {
            "name": {"hit_rate": name, "n": 355},
            "topic": {"hit_rate": topic, "n": 355},
        },
        "stranded_skills": [f"sk-{i}" for i in range(stranded)],
    }


def _gold(hits: int, total: int = 8) -> dict[str, object]:
    return {"gold_hit": hits, "gold_hit_total": total}


def _baseline_pass() -> dict[str, object]:
    # Exactly at baseline → passes (drop must EXCEED tolerance to fail).
    return _audit(0.901, 0.921, 18)


# --------------------------------------------------------------------------- #
# compare(): per-metric regression                                            #
# --------------------------------------------------------------------------- #


def test_pass_at_baseline() -> None:
    failures, notices = compare(_baseline_pass(), _gold(7), BASELINES)
    assert failures == []
    assert notices == []


def test_within_tolerance_passes() -> None:
    # name dips 0.015 (< 0.02 tolerance) → no failure.
    failures, _ = compare(_audit(0.886, 0.921, 18), _gold(7), BASELINES)
    assert failures == []


def test_name_probe_regression_fails() -> None:
    # name drops 0.05 below baseline → fail.
    failures, _ = compare(_audit(0.851, 0.921, 18), _gold(7), BASELINES)
    assert len(failures) == 1
    assert "name probe hit_rate REGRESSED" in failures[0]


def test_topic_probe_regression_fails() -> None:
    failures, _ = compare(_audit(0.901, 0.80, 18), _gold(7), BASELINES)
    assert any("topic probe hit_rate REGRESSED" in f for f in failures)


def test_stranded_regression_fails() -> None:
    failures, _ = compare(_audit(0.901, 0.921, 23), _gold(7), BASELINES)
    assert any("stranded skill count REGRESSED" in f for f in failures)
    assert any("23 > baseline 18" in f for f in failures)


def test_gold_hit_regression_fails() -> None:
    failures, _ = compare(_baseline_pass(), _gold(5), BASELINES)
    assert any("gold_hit REGRESSED" in f for f in failures)


def test_multiple_regressions_reported() -> None:
    failures, _ = compare(_audit(0.80, 0.80, 30), _gold(3), BASELINES)
    assert len(failures) == 4


# --------------------------------------------------------------------------- #
# compare(): improvements pass with notices                                   #
# --------------------------------------------------------------------------- #


def test_improvement_passes_with_notice() -> None:
    failures, notices = compare(_audit(0.97, 0.97, 10), _gold(8), BASELINES)
    assert failures == []
    assert any("IMPROVED" in n for n in notices)
    assert any("name probe" in n for n in notices)
    assert any("stranded" in n for n in notices)
    assert any("gold_hit" in n for n in notices)


# --------------------------------------------------------------------------- #
# compare(): malformed inputs                                                 #
# --------------------------------------------------------------------------- #


def test_missing_probe_type_raises() -> None:
    with pytest.raises(RegressionError, match="by_probe_type.name"):
        compare({"by_probe_type": {}, "stranded_skills": []}, _gold(7), BASELINES)


def test_missing_stranded_raises() -> None:
    audit = {"by_probe_type": _baseline_pass()["by_probe_type"]}
    with pytest.raises(RegressionError, match="stranded_skills"):
        compare(audit, _gold(7), BASELINES)


def test_missing_gold_hit_raises() -> None:
    with pytest.raises(RegressionError, match="gold_hit"):
        compare(_baseline_pass(), {}, BASELINES)


# --------------------------------------------------------------------------- #
# run(): end-to-end against tmp run files                                     #
# --------------------------------------------------------------------------- #


def _write(runs: Path, prefix: str, stamp: str, payload: dict[str, object]) -> None:
    (runs / f"{prefix}{stamp}.json").write_text(json.dumps(payload))


def test_run_picks_latest_and_passes(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    baselines = tmp_path / "baselines.json"
    baselines.write_text(json.dumps(BASELINES))

    # Older regression file + newer passing file — must use the newer one.
    _write(runs, "retrieval-audit-", "2026-06-11T00-00-00Z", _audit(0.10, 0.10, 99))
    _write(runs, "retrieval-audit-", "2026-06-11T02-00-00Z", _baseline_pass())
    _write(runs, "gold-hit-", "2026-06-11T02-00-00Z", _gold(7))

    assert run(runs, baselines) == 0


def test_run_detects_regression(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    baselines = tmp_path / "baselines.json"
    baselines.write_text(json.dumps(BASELINES))

    _write(runs, "retrieval-audit-", "2026-06-11T02-00-00Z", _audit(0.851, 0.921, 18))
    _write(runs, "gold-hit-", "2026-06-11T02-00-00Z", _gold(7))

    assert run(runs, baselines) == 1


def test_run_missing_audit_file_errors(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    baselines = tmp_path / "baselines.json"
    baselines.write_text(json.dumps(BASELINES))
    _write(runs, "gold-hit-", "2026-06-11T02-00-00Z", _gold(7))

    with pytest.raises(RegressionError, match="retrieval-audit-"):
        run(runs, baselines)


def test_run_missing_gold_file_errors(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    baselines = tmp_path / "baselines.json"
    baselines.write_text(json.dumps(BASELINES))
    _write(runs, "retrieval-audit-", "2026-06-11T02-00-00Z", _baseline_pass())

    with pytest.raises(RegressionError, match="gold-hit-"):
        run(runs, baselines)


# ---------------------------------------------------------------------------
# Per-phase hit-rate floors (added with ops-phase probing)
# ---------------------------------------------------------------------------


def _audit_with_phases(phases: dict[str, float]) -> dict:
    return {
        "by_probe_type": {
            "name": {"hit_rate": 0.95, "n": 100},
            "topic": {"hit_rate": 0.97, "n": 100},
        },
        "by_phase": {p: {"hit_rate": r, "n": 100} for p, r in phases.items()},
        "stranded_skills": [],
    }


def _baselines_with_floors(floors: dict[str, float]) -> dict:
    return {
        "name_probe_hit_rate": 0.95,
        "topic_probe_hit_rate": 0.97,
        "stranded_count": 0,
        "gold_hit": 8,
        "gold_hit_total": 8,
        "tolerance": 0.02,
        "phase_hit_rate_floors": floors,
    }


def test_phase_floor_regression_fails() -> None:
    failures, _ = compare(
        _audit_with_phases({"build": 0.96, "ship": 0.85}),
        {"gold_hit": 8, "gold_hit_total": 8},
        _baselines_with_floors({"build": 0.96, "ship": 0.96}),
    )
    assert any("'ship'" in f and "REGRESSED" in f for f in failures)
    assert not any("'build'" in f for f in failures)


def test_phase_floor_within_tolerance_passes() -> None:
    failures, _ = compare(
        _audit_with_phases({"ship": 0.945}),
        {"gold_hit": 8, "gold_hit_total": 8},
        _baselines_with_floors({"ship": 0.96}),
    )
    assert failures == []


def test_phase_floor_missing_measurement_fails() -> None:
    """A floored phase vanishing from the audit must fail, not pass silently."""
    failures, _ = compare(
        _audit_with_phases({"build": 0.96}),
        {"gold_hit": 8, "gold_hit_total": 8},
        _baselines_with_floors({"ship": 0.96}),
    )
    assert any("'ship'" in f and "no audit measurement" in f for f in failures)


def test_phase_floor_improvement_notices() -> None:
    _, notices = compare(
        _audit_with_phases({"ship": 0.99}),
        {"gold_hit": 8, "gold_hit_total": 8},
        _baselines_with_floors({"ship": 0.96}),
    )
    assert any("phase_hit_rate_floors.ship" in n for n in notices)


def test_no_floors_key_is_backward_compatible() -> None:
    baselines = _baselines_with_floors({})
    del baselines["phase_hit_rate_floors"]
    failures, _ = compare(
        _audit_with_phases({"build": 0.5}),
        {"gold_hit": 8, "gold_hit_total": 8},
        baselines,
    )
    assert failures == []
