"""Unit tests for the semantic review gate (Gate 1.5) — the pure validator.

`validate_review_verdicts` only reads each skill's bytes (for the freshness
hash) and `review.yaml`; it does NOT re-validate skill schema (Gate 1's job).
So fixtures stay minimal — any bytes for the skill file, a dict for the verdict.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from agentalloy import pack_validation
from agentalloy.pack_validation import (
    ReviewVerdict,
    skill_file_sha256,
    validate_review_verdicts,
)

SKILL_FILE = "demo-skill.yaml"
SKILL_ID = "demo-skill"
SKILL_BYTES = b"skill_id: demo-skill\ncanonical_name: Demo\n"


def _pack_with_skill(tmp_path: Path, skill_bytes: bytes = SKILL_BYTES) -> Path:
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / SKILL_FILE).write_bytes(skill_bytes)
    return pack


def _entries() -> list[dict[str, Any]]:
    return [{"skill_id": SKILL_ID, "file": SKILL_FILE}]


def _write_review(pack: Path, review: dict[str, Any]) -> None:
    (pack / "review.yaml").write_text(yaml.safe_dump(review), encoding="utf-8")


def _good_verdict(pack: Path, **overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "skill_id": SKILL_ID,
        "target_hash": skill_file_sha256(pack, SKILL_FILE),
        "verdict": "approve",
        "blocking_issues": [],
        "checks": {"R1": "pass", "R3": "pass"},
        "reviewer": {"model": "claude-sonnet-5", "harness": "claude-code", "mode": "independent"},
        "source_refs": [],
        "created_at": "2026-07-13T00:00:00Z",
    }
    entry.update(overrides)
    return {"schema_version": 1, "reviews": [entry]}


# --- AC 1: missing verdict -------------------------------------------------


def test_missing_review_file_blocks(tmp_path: Path) -> None:
    pack = _pack_with_skill(tmp_path)  # no review.yaml written
    result = validate_review_verdicts(pack, _entries())
    assert not result.ok
    assert "missing" in result.format_errors().lower()


def test_review_present_but_no_entry_for_skill_blocks(tmp_path: Path) -> None:
    pack = _pack_with_skill(tmp_path)
    _write_review(pack, {"schema_version": 1, "reviews": []})
    result = validate_review_verdicts(pack, _entries())
    assert not result.ok
    assert "no review verdict for skill" in result.format_errors()


# --- AC 4: valid approve passes -------------------------------------------


def test_valid_approve_passes(tmp_path: Path) -> None:
    pack = _pack_with_skill(tmp_path)
    _write_review(pack, _good_verdict(pack))
    result = validate_review_verdicts(pack, _entries())
    assert result.ok
    assert result.errors == []


# --- AC 2: stale hash ------------------------------------------------------


def test_stale_hash_blocks_after_edit(tmp_path: Path) -> None:
    pack = _pack_with_skill(tmp_path)
    _write_review(pack, _good_verdict(pack))
    # Edit the skill by one byte after the verdict was authored.
    (pack / SKILL_FILE).write_bytes(SKILL_BYTES + b"# edited\n")
    result = validate_review_verdicts(pack, _entries())
    assert not result.ok
    assert "stale review" in result.format_errors()


def test_wrong_hash_blocks(tmp_path: Path) -> None:
    pack = _pack_with_skill(tmp_path)
    _write_review(pack, _good_verdict(pack, target_hash="sha256:deadbeef"))
    result = validate_review_verdicts(pack, _entries())
    assert not result.ok
    assert "stale review" in result.format_errors()


# --- AC 3: non-approving verdicts -----------------------------------------


@pytest.mark.parametrize(
    ("overrides", "needle"),
    [
        ({"verdict": "revise"}, "not 'approve'"),
        ({"verdict": "reject"}, "not 'approve'"),
        ({"blocking_issues": ["hallucinated API"]}, "blocking issue"),
        ({"checks": {}}, "no 'checks'"),
        ({"checks": {"R1": "pass", "R3": "fail"}}, "checks failed"),
    ],
)
def test_non_approving_verdict_blocks(
    tmp_path: Path, overrides: dict[str, Any], needle: str
) -> None:
    pack = _pack_with_skill(tmp_path)
    _write_review(pack, _good_verdict(pack, **overrides))
    result = validate_review_verdicts(pack, _entries())
    assert not result.ok
    assert needle in result.format_errors()


# --- DK6: independence lever ----------------------------------------------


def test_self_mode_passes_by_default(tmp_path: Path) -> None:
    pack = _pack_with_skill(tmp_path)
    _write_review(pack, _good_verdict(pack, reviewer={"mode": "self"}))
    result = validate_review_verdicts(pack, _entries())
    assert result.ok


def test_self_mode_blocked_when_independent_required(tmp_path: Path) -> None:
    pack = _pack_with_skill(tmp_path)
    _write_review(pack, _good_verdict(pack, reviewer={"mode": "self"}))
    result = validate_review_verdicts(pack, _entries(), require_independent=True)
    assert not result.ok
    assert "independent review" in result.format_errors()


def test_independent_mode_passes_when_required(tmp_path: Path) -> None:
    pack = _pack_with_skill(tmp_path)
    _write_review(pack, _good_verdict(pack, reviewer={"mode": "independent"}))
    result = validate_review_verdicts(pack, _entries(), require_independent=True)
    assert result.ok


# --- malformed review.yaml -------------------------------------------------


def test_unparseable_review_blocks(tmp_path: Path) -> None:
    pack = _pack_with_skill(tmp_path)
    (pack / "review.yaml").write_text("{ not: valid: yaml", encoding="utf-8")
    result = validate_review_verdicts(pack, _entries())
    assert not result.ok


def test_reviews_not_a_list_blocks(tmp_path: Path) -> None:
    pack = _pack_with_skill(tmp_path)
    (pack / "review.yaml").write_text("reviews: not-a-list\n", encoding="utf-8")
    result = validate_review_verdicts(pack, _entries())
    assert not result.ok
    assert "'reviews' list" in result.format_errors()


# --- AC 10: aggregated shape ----------------------------------------------


def test_aggregates_per_skill_like_gate_one(tmp_path: Path) -> None:
    pack = _pack_with_skill(tmp_path)
    (pack / "second.yaml").write_bytes(b"skill_id: second\n")
    entries = [
        {"skill_id": SKILL_ID, "file": SKILL_FILE},
        {"skill_id": "second", "file": "second.yaml"},
    ]
    # Only the first skill has a verdict.
    _write_review(pack, _good_verdict(pack))
    result = validate_review_verdicts(pack, entries)
    assert not result.ok
    # One SkillValidationError, for the un-reviewed 'second' skill.
    assert [e.skill_id for e in result.errors] == ["second"]


def test_absent_skill_file_is_not_our_error(tmp_path: Path) -> None:
    pack = _pack_with_skill(tmp_path)
    _write_review(pack, _good_verdict(pack))
    entries = [*_entries(), {"skill_id": "ghost", "file": "ghost.yaml"}]  # ghost.yaml doesn't exist
    result = validate_review_verdicts(pack, entries)
    assert result.ok  # ghost is skipped (Gate 1 / manifest reader owns absent files)


# --- ReviewVerdict tolerance ----------------------------------------------


def test_from_entry_tolerates_missing_keys() -> None:
    v = ReviewVerdict.from_entry({"skill_id": "x"})
    assert v.skill_id == "x"
    assert v.verdict == ""
    assert v.checks == {}
    assert v.reviewer_mode == ""
    assert v.blocking_issues == []


# --- AC 5: no LLM / network in the gate ------------------------------------


def test_gate_module_has_no_llm_or_network_imports() -> None:
    src = Path(pack_validation.__file__).read_text(encoding="utf-8")
    assert "lm_client" not in src
    assert "httpx" not in src
    assert "requests" not in src
    assert "authoring" not in src
