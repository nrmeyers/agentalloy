"""Task 02: the compound-engineering codify gate on the qa->ship edge.

Exercises the real CLI forward-gate path (``_forward_gate_blocks``) against the
shipped ``sdd-verify-and-review`` skill, plus the prose<->gate self-consistency
(TC3) and the migration note (TC8).
"""

from __future__ import annotations

from pathlib import Path

from agentalloy.install.subcommands.phase import _forward_gate_blocks
from agentalloy.signals.invariants import check_prose, derive_invariants, load_shipped_skill

SLUG = "feat-x"


def _qa_ready(root: Path) -> None:
    """A repo whose qa exit artifact + work-item are in place — everything the
    qa->ship gate needs EXCEPT the codify lesson."""
    (root / ".agentalloy" / "contracts" / "active" / "qa").mkdir(parents=True, exist_ok=True)
    (root / ".agentalloy" / "contracts" / "active" / "qa" / f"{SLUG}.md").write_text(
        "---\nphase: qa\n---\n", encoding="utf-8"
    )
    (root / "docs" / "qa").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "qa" / f"{SLUG}.md").write_text(
        "# qa\n\n## Checks\n\nall green\n\n## Review\n\nclean\n", encoding="utf-8"
    )


def test_tc1_gate_blocks_qa_to_ship_without_lesson(tmp_path: Path):
    _qa_ready(tmp_path)
    blocked, advisories = _forward_gate_blocks("qa", "ship", tmp_path, None)
    assert blocked is True
    # sanity: the qa doc leaves are satisfied, so it's the codify leaf blocking
    assert isinstance(advisories, list)


def test_tc1_gate_allows_once_lesson_recorded(tmp_path: Path):
    _qa_ready(tmp_path)
    (tmp_path / "docs" / "solutions").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "solutions" / f"{SLUG}.md").write_text(
        "# lesson\n\nwhat worked\n", encoding="utf-8"
    )
    blocked, _ = _forward_gate_blocks("qa", "ship", tmp_path, None)
    assert blocked is False


def test_tc2_stale_lesson_for_other_task_still_blocks(tmp_path: Path):
    _qa_ready(tmp_path)
    (tmp_path / "docs" / "solutions").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "solutions" / "some-old-task.md").write_text("# old", encoding="utf-8")
    blocked, _ = _forward_gate_blocks("qa", "ship", tmp_path, None)
    assert blocked is True


def test_tc3_shipped_prose_gate_self_consistent(tmp_path: Path):
    shipped = load_shipped_skill("sdd-verify-and-review")
    assert shipped is not None
    invariants = derive_invariants(shipped)
    assert "docs/solutions/" in invariants  # the codify coupling token is derived
    # the shipped prose retains every load-bearing token (no override-rejection warning)
    assert check_prose(shipped["raw_prose"], invariants) == []


def test_tc8_migration_note_present(tmp_path: Path):
    shipped = load_shipped_skill("sdd-verify-and-review")
    assert shipped is not None
    summary = shipped.get("change_summary", "")
    assert "docs/solutions/" in summary
    assert "MIGRATION" in summary
