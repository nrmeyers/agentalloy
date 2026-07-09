"""Unit tests for the ``lessons_recorded`` gate predicate and the shared
``resolve_current_contract`` resolver.

Compound-engineering bridge, build task 01. Covers AC 1 (gate blocks the advance
to ship without a lesson), AC 2 (a stale lesson for another task does not satisfy
it), plus the resolver's cursor/containment/fan-out behavior and the proxy
delegation.
"""

from __future__ import annotations

from pathlib import Path

from agentalloy.contracts import resolve_current_contract
from agentalloy.signals.predicates import (
    PREDICATES,
    PredicateContext,
    PredicateResult,
    eval_lessons_recorded,
    evaluate_predicate,
)

MET = PredicateResult.MET
NOT_MET = PredicateResult.NOT_MET
UNKNOWN = PredicateResult.UNKNOWN


def _write(p: Path, text: str = "x") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _qa_contract(root: Path, slug: str) -> None:
    _write(root / ".agentalloy" / "contracts" / "qa" / f"{slug}.md", "---\nphase: qa\n---\n")


def _ctx(root: Path, phase: str | None = "qa") -> PredicateContext:
    return PredicateContext(project_root=root, current_phase=phase)


def test_registered_and_reachable(tmp_path: Path):
    assert "lessons_recorded" in PREDICATES
    _qa_contract(tmp_path, "feat-x")
    # reachable through the generic dispatcher too
    assert evaluate_predicate("lessons_recorded", {}, _ctx(tmp_path)) is NOT_MET


def test_tc1_blocks_then_passes(tmp_path: Path):
    _qa_contract(tmp_path, "feat-x")
    # no lesson yet -> NOT_MET (blocks the qa->ship advance)
    assert eval_lessons_recorded({}, _ctx(tmp_path)) is NOT_MET
    _write(tmp_path / "docs" / "solutions" / "feat-x.md", "# lesson")
    assert eval_lessons_recorded({}, _ctx(tmp_path)) is MET


def test_tc2_stale_other_slug_does_not_satisfy(tmp_path: Path):
    _qa_contract(tmp_path, "feat-x")
    _write(tmp_path / "docs" / "solutions" / "other.md", "# a prior task's lesson")
    # a different task's lesson must NOT satisfy the gate for feat-x (the stale-file guard)
    assert eval_lessons_recorded({}, _ctx(tmp_path)) is NOT_MET


def test_no_workitem_is_unknown(tmp_path: Path):
    # no qa contract at all -> cannot resolve a slug -> UNKNOWN (fail-open, never blocks)
    assert eval_lessons_recorded({}, _ctx(tmp_path)) is UNKNOWN


def test_ambiguous_fanout_is_unknown(tmp_path: Path):
    # ≥2 contracts, no cursor -> the strict resolver yields no single work-item ->
    # UNKNOWN (fail-open). The gate never blocks against a guessed slug. In normal
    # flow the cursor is seeded on phase entry (see the next test), so this is the
    # fail-safe floor, not the common path.
    _qa_contract(tmp_path, "feat-x")
    _qa_contract(tmp_path, "feat-y")
    assert eval_lessons_recorded({}, _ctx(tmp_path)) is UNKNOWN


def test_phase_entry_seeds_gate_scope(tmp_path: Path):
    # Entering qa seeds the cursor to the first work-item (filename order); the gate
    # then resolves THAT slug deterministically — blocks until its lesson exists.
    from agentalloy.signals.skill_loader import (  # type: ignore[reportPrivateUsage]
        _write_phase_atomic,
    )

    _qa_contract(tmp_path, "01-alpha")
    _qa_contract(tmp_path, "02-beta")
    (tmp_path / ".agentalloy" / "phase").write_text("phase: build\n")  # enter qa from elsewhere
    _write_phase_atomic(tmp_path, "qa")
    # seeded to 01-alpha; its lesson is absent -> NOT_MET (blocks), not UNKNOWN
    assert eval_lessons_recorded({}, _ctx(tmp_path)) is NOT_MET
    _write(tmp_path / "docs" / "solutions" / "01-alpha.md", "# lesson")
    assert eval_lessons_recorded({}, _ctx(tmp_path)) is MET


def test_cursor_selects_slug(tmp_path: Path):
    _qa_contract(tmp_path, "feat-x")
    _qa_contract(tmp_path, "feat-y")
    _write(tmp_path / ".agentalloy" / "cursor", "qa/feat-y.md")
    _write(tmp_path / "docs" / "solutions" / "feat-y.md", "# lesson")
    # cursor pins feat-y; its lesson exists -> MET even though feat-x has none
    assert eval_lessons_recorded({}, _ctx(tmp_path)) is MET
    # repoint the cursor to feat-x, whose lesson is absent -> NOT_MET
    _write(tmp_path / ".agentalloy" / "cursor", "qa/feat-x.md")
    assert eval_lessons_recorded({}, _ctx(tmp_path)) is NOT_MET


def test_phase_arg_overrides_ctx(tmp_path: Path):
    _qa_contract(tmp_path, "feat-x")
    _write(tmp_path / "docs" / "solutions" / "feat-x.md", "# lesson")
    # explicit phase arg is honored when ctx.current_phase is None
    assert eval_lessons_recorded({"phase": "qa"}, _ctx(tmp_path, None)) is MET


def test_resolver_cursor_containment(tmp_path: Path):
    # a cursor escaping the contracts tree must be ignored (containment guard),
    # falling back to the single qa contract
    _qa_contract(tmp_path, "feat-x")
    _write(tmp_path / ".agentalloy" / "cursor", "../../../etc/passwd")
    _cid, path = resolve_current_contract(tmp_path, "qa")
    assert path is not None and path.name == "feat-x.md"


def test_proxy_wrapper_matches_resolver(tmp_path: Path):
    # the refactor: the proxy wrapper must resolve identically to the shared function
    from agentalloy.api.proxy_signal import _resolve_current_contract

    _qa_contract(tmp_path, "feat-x")
    assert _resolve_current_contract(tmp_path, "qa") == resolve_current_contract(tmp_path, "qa")
