"""Task 02: the compound-engineering codify gate on the qa->ship edge.

Exercises the real CLI forward-gate path (``_forward_gate_blocks``) against the
shipped ``sdd-verify-and-review`` skill, plus the prose<->gate self-consistency
(TC3) and the migration note (TC8).
"""

from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path

from agentalloy.install.subcommands.phase import _forward_gate_blocks
from agentalloy.lessons_artifact import LESSON_NAME, LESSON_PHASE
from agentalloy.signals.invariants import check_prose, derive_invariants, load_shipped_skill
from agentalloy.signals.predicates import (
    PredicateContext,
    PredicateResult,
    eval_lessons_recorded,
)

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
    # The codify coupling token. Now the store-artifact phrasing, not the old
    # docs/solutions/ path: the lesson is a store artifact (phase=qa,
    # name=solution), so a path token would name a location post-migration repos
    # no longer write to.
    assert "the solutions artifact" in invariants
    # the shipped prose retains every load-bearing token (no override-rejection warning)
    assert check_prose(shipped["raw_prose"], invariants) == []


def test_tc8_migration_note_present(tmp_path: Path):
    shipped = load_shipped_skill("sdd-verify-and-review")
    assert shipped is not None
    summary = shipped.get("change_summary", "")
    assert "docs/solutions/" in summary
    assert "MIGRATION" in summary


# --- store-backed lesson (the docs/solutions -> artifact migration) -----------


class _FakeStore:
    """Minimal ``list_artifacts`` stand-in — the only store call the codify gate
    makes. ``rows`` is keyed (phase, slug, name)."""

    def __init__(self, rows: list[tuple[str, str, str]], *, error: bool = False):
        self._rows = rows
        self._error = error

    def list_artifacts(self, phase, *, slug=None, name_glob=None, status="active"):
        if self._error:
            raise RuntimeError("store unreachable")
        return [
            {"phase": p, "slug": s, "name": n, "content": "lesson body", "status": "active"}
            for (p, s, n) in self._rows
            if p == phase
            and (slug is None or s == slug)
            and (name_glob is None or fnmatch(n, name_glob))
        ]


def _ctx(root: Path, store):
    return PredicateContext(project_root=root, current_phase="qa", store=store)


def test_store_lesson_satisfies_gate_without_any_file(tmp_path: Path):
    """The migration's whole point: a stored lesson clears the gate with nothing
    on disk under docs/solutions/."""
    _qa_ready(tmp_path)
    store = _FakeStore([(LESSON_PHASE, SLUG, LESSON_NAME)])
    assert eval_lessons_recorded({"phase": "qa"}, _ctx(tmp_path, store)) == PredicateResult.MET
    assert not (tmp_path / "docs" / "solutions").exists()


def test_store_lesson_is_slug_scoped(tmp_path: Path):
    """A stored lesson for a DIFFERENT task must not satisfy this task's gate —
    the same staleness rule TC2 pins for the disk path."""
    _qa_ready(tmp_path)
    store = _FakeStore([(LESSON_PHASE, "some-old-task", LESSON_NAME)])
    assert eval_lessons_recorded({"phase": "qa"}, _ctx(tmp_path, store)) == PredicateResult.NOT_MET


def test_disk_lesson_still_satisfies_gate_when_store_has_none(tmp_path: Path):
    """Pre-migration repos are not stranded."""
    _qa_ready(tmp_path)
    (tmp_path / "docs" / "solutions").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "solutions" / f"{SLUG}.md").write_text("# lesson", encoding="utf-8")
    store = _FakeStore([])
    assert eval_lessons_recorded({"phase": "qa"}, _ctx(tmp_path, store)) == PredicateResult.MET


def test_store_error_falls_back_to_disk(tmp_path: Path):
    """An unreachable store must degrade to the disk check, never hard-block a
    repo that has recorded its lesson."""
    _qa_ready(tmp_path)
    (tmp_path / "docs" / "solutions").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "solutions" / f"{SLUG}.md").write_text("# lesson", encoding="utf-8")
    store = _FakeStore([], error=True)
    assert eval_lessons_recorded({"phase": "qa"}, _ctx(tmp_path, store)) == PredicateResult.MET


def test_lesson_name_is_not_swept_up_by_the_qa_md_glob(tmp_path: Path):
    """Regression: the qa exit gate globs `name: "*.md"` and artifact_contains
    requires EVERY matching row to carry ## Checks/## Review. If the lesson were
    named solution.md, writing it would break the gate beside it — so the name
    carries no .md suffix and the glob must miss it."""
    assert not fnmatch(LESSON_NAME, "*.md")
