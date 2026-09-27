"""The nightly skill-selection comparator (eval.check_selection_regression)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from eval import check_selection_regression as check


def _report(hit_tasks: list[str], matched: int, errors: int = 0) -> dict[str, Any]:
    tasks = ["a", "b", "c", "d"]
    return {
        "total": len(tasks),
        "hits": len(hit_tasks),
        "matched_total": 3,
        "matched_hits": matched,
        "errors": errors,
        "results": [{"task_id": t, "hit": t in hit_tasks} for t in tasks],
    }


BASE: dict[str, Any] = {
    "floors": {"hits": 2, "matched_hits": 2},
    "max_errors": 1,
    "measured": {"hits": 3, "total": 4, "hit_tasks": ["a", "b", "c"]},
}


def test_within_floors_passes() -> None:
    assert check.judge(_report(["a", "b"], matched=2), BASE) == []


def test_each_floor_fails_on_its_own() -> None:
    assert len(check.judge(_report(["a"], matched=2), BASE)) == 1
    assert len(check.judge(_report(["a", "b"], matched=1), BASE)) == 1
    assert len(check.judge(_report(["a", "b"], matched=2, errors=2), BASE)) == 1


def test_flips_are_reported_both_ways() -> None:
    lost, gained = check.flips(_report(["a", "d"], matched=2), BASE)
    assert lost == ["b", "c"]
    assert gained == ["d"]


def test_main_exit_codes(tmp_path: Path, monkeypatch: Any) -> None:
    baselines = tmp_path / "baselines.json"
    baselines.write_text(json.dumps(BASE))
    monkeypatch.setattr(check, "BASELINES", baselines)
    ok = tmp_path / "ok.json"
    ok.write_text(json.dumps(_report(["a", "b", "c"], matched=3)))
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(_report([], matched=0)))
    assert check.main([str(ok)]) == 0
    assert check.main([str(bad)]) == 1
    assert check.main([str(tmp_path / "missing.json")]) == 2
