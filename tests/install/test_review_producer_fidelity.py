"""Slice-2 fidelity: the review producer's prescription ↔ slice-1's frozen gate.

`sys-skill-review-verdict.md` (the producer meta skill) tells the operator's agent
how to emit `review.yaml`. This test proves the shape it prescribes is *exactly* the
shape `validate_review_verdicts` (slice 1, shipped) accepts, and pins the
anti-rubber-stamp coverage bar the gate itself cannot enforce (DK4/DK10): the gate
blocks only on a `fail` check, so an all-`na` or partial map passes it — coverage is
the producer's job, verified here, not the backend's.

No LLM, no network: a golden verdict authored to the producer's prescription is a
deterministic fixture, not a model run (AC 7 is the fidelity contract, DK14).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

import agentalloy
from agentalloy.pack_validation import skill_file_sha256, validate_review_verdicts
from tests.install.test_install_local_pack import _write_skill_yaml

_PACKS = Path(agentalloy.__file__).parent / "_packs"
_RULES_DOC = _PACKS / "meta" / "sys-skill-authoring-rules.md"
_PRODUCER_DOC = _PACKS / "meta" / "sys-skill-review-verdict.md"

SKILL_ID = "demo-skill"


def _rule_ids() -> list[str]:
    """The R-id set the producer must cover — parsed from the rules doc, not a
    literal (DK11: a future R10 must be covered by re-reading, not editing a test)."""
    text = _RULES_DOC.read_text(encoding="utf-8")
    ids = re.findall(r"^##\s+(R\d+)\b", text, flags=re.MULTILINE)
    # Preserve document order, de-dup.
    seen: dict[str, None] = {}
    for r in ids:
        seen.setdefault(r, None)
    return list(seen)


def _pack_with_skill(tmp_path: Path) -> tuple[Path, list[dict[str, Any]]]:
    _write_skill_yaml(tmp_path, SKILL_ID)
    entries = [{"skill_id": SKILL_ID, "file": f"{SKILL_ID}.yaml"}]
    return tmp_path, entries


def _write_verdict(pack: Path, *, checks: dict[str, str], **overrides: Any) -> None:
    entry: dict[str, Any] = {
        "skill_id": SKILL_ID,
        "target_hash": skill_file_sha256(pack, f"{SKILL_ID}.yaml"),
        "verdict": "approve",
        "blocking_issues": [],
        "checks": checks,
        "reviewer": {"model": "claude-sonnet-5", "harness": "claude-code", "mode": "self"},
        "source_refs": [],
        "created_at": "2026-07-14T00:00:00Z",
    }
    entry.update(overrides)
    (pack / "review.yaml").write_text(
        yaml.safe_dump({"schema_version": 1, "reviews": [entry]}), encoding="utf-8"
    )


def _full_checks() -> dict[str, str]:
    """A defended full-coverage map: applies most rules, `na`s the legitimately
    inapplicable ones for a hand-authored non-sourced skill (see the producer doc)."""
    na = {"R1", "R5", "R9"}  # tiered sourcing / date-stamp / deprecation — N/A here
    return {r: ("na" if r in na else "pass") for r in _rule_ids()}


# --- AC 7: the producer's shape is the shape the gate accepts -----------------


def test_golden_verdict_accepted_by_real_gate(tmp_path: Path) -> None:
    pack, entries = _pack_with_skill(tmp_path)
    _write_verdict(pack, checks=_full_checks())
    result = validate_review_verdicts(pack, entries)
    assert result.ok, result.format_errors()


def test_coverage_is_complete_over_r1_through_r9(tmp_path: Path) -> None:
    pack, _ = _pack_with_skill(tmp_path)
    checks = _full_checks()
    # The rules doc has R1–R9 today; the producer must cover every one.
    assert set(checks) == set(_rule_ids())
    assert len(_rule_ids()) == 9, "rules doc drifted from R1–R9; update the producer + spec text"


# --- DK4/DK10: coverage is the producer's job, NOT the gate's -----------------


def test_partial_map_passes_gate_but_fails_coverage(tmp_path: Path) -> None:
    """Dropping a rule still validates at Gate 1.5 (the gate blocks only on `fail`)
    — proving the coverage guarantee lives in the producer, checked here."""
    pack, entries = _pack_with_skill(tmp_path)
    partial = _full_checks()
    dropped = partial.pop("R7")  # remove one rule
    assert dropped  # sanity: R7 existed
    _write_verdict(pack, checks=partial)
    # Gate is satisfied...
    assert validate_review_verdicts(pack, entries).ok
    # ...but coverage (the producer's contract) is not.
    assert set(partial) != set(_rule_ids())


def test_all_na_passes_gate_documenting_the_rubber_stamp_gap(tmp_path: Path) -> None:
    """An all-`na` map is exactly the rubber-stamp the gate cannot detect (DK10).
    It passes Gate 1.5; the producer prose — not the backend — forbids it."""
    pack, entries = _pack_with_skill(tmp_path)
    _write_verdict(pack, checks={r: "na" for r in _rule_ids()})
    assert validate_review_verdicts(pack, entries).ok


# --- AC 2 regression: hash freshness protects against post-review edits --------


def test_edit_after_verdict_goes_stale(tmp_path: Path) -> None:
    pack, entries = _pack_with_skill(tmp_path)
    _write_verdict(pack, checks=_full_checks())
    assert validate_review_verdicts(pack, entries).ok
    # One-byte edit to the skill after the verdict was authored.
    skill = pack / f"{SKILL_ID}.yaml"
    skill.write_bytes(skill.read_bytes() + b"# edited\n")
    result = validate_review_verdicts(pack, entries)
    assert not result.ok
    assert "stale review" in result.format_errors()


def test_fail_check_blocks(tmp_path: Path) -> None:
    """A `fail` check is incompatible with approve — the producer must re-author,
    not downgrade the finding (defense that the fixture can't approve-with-fail)."""
    pack, entries = _pack_with_skill(tmp_path)
    checks = _full_checks()
    checks["R4"] = "fail"
    _write_verdict(pack, checks=checks)
    result = validate_review_verdicts(pack, entries)
    assert not result.ok
    assert "checks failed" in result.format_errors()


# --- AC 5 regression: no LLM / network in the producer or this test -----------


def test_producer_and_test_have_no_llm_or_network() -> None:
    producer = _PRODUCER_DOC.read_text(encoding="utf-8")
    for needle in ("lm_client", "11434", "qwen3.6", "LM Studio", "httpx", "requests.get"):
        assert needle not in producer, f"producer references {needle!r}"
    # The producer must explicitly disambiguate the "review YAML" name collision
    # with sys-skill-transform-contract (it names the phrase only to warn against
    # it) — assert the warning is present, not that the phrase is absent.
    assert "never call" in producer and "review YAML" in producer


def test_producer_skill_parses_as_a_meta_skill() -> None:
    """The new meta skill must parse via the bootstrap loader (well-formed header)."""
    from agentalloy.skill_md.parser import parse_file

    parsed = parse_file(_PRODUCER_DOC)
    assert parsed.skill_id == "sys-skill-review-verdict"
    assert parsed.category == "tooling"
