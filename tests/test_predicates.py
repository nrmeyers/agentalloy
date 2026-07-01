"""Per-predicate unit tests for agentalloy.signals.predicates."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from agentalloy.signals.predicates import (
    PredicateContext,
    PredicateResult,
    approval_marker_path,
    approval_required,
    eval_approval_recorded,
    eval_artifact_absent,
    eval_artifact_contains,
    eval_artifact_exists,
    eval_artifact_newer_than,
    eval_artifact_size_min,
    eval_build_contract_tag_focus,
    eval_build_contracts_cover_tasks,
    eval_contract_exists,
    eval_contract_has_tags,
    eval_file_type_active,
    eval_git_state,
    eval_phase_in,
    eval_phase_not_in,
    eval_tests_present,
    eval_tool_use_about_to_fire,
    evaluate_predicate,
    section_completeness,
)

MET = PredicateResult.MET
NOT_MET = PredicateResult.NOT_MET
UNKNOWN = PredicateResult.UNKNOWN


def _ctx(tmp_path: Path, **kwargs: Any) -> PredicateContext:
    defaults: dict[str, Any] = dict(project_root=tmp_path, current_phase="build")
    defaults.update(kwargs)
    return PredicateContext(**defaults)


# ---------------------------------------------------------------------------
# embed-failure diagnostics sink
# ---------------------------------------------------------------------------


def test_embed_failed_defaults_false(tmp_path: Path) -> None:
    """A fresh context reports no embed failure."""
    assert _ctx(tmp_path).embed_failed is False


def test_record_embed_failure_sets_flag(tmp_path: Path) -> None:
    """record_embed_failure flips embed_failed and is idempotent."""
    ctx = _ctx(tmp_path)
    ctx.record_embed_failure()
    assert ctx.embed_failed is True
    ctx.record_embed_failure()  # idempotent — still True, no error
    assert ctx.embed_failed is True


def test_embed_failure_is_per_context(tmp_path: Path) -> None:
    """The sink is per-instance: one context's failure doesn't leak to another."""
    failed = _ctx(tmp_path)
    failed.record_embed_failure()
    assert _ctx(tmp_path).embed_failed is False


# ---------------------------------------------------------------------------
# artifact_exists / artifact_absent
# ---------------------------------------------------------------------------


def test_artifact_exists_found(tmp_path: Path):
    (tmp_path / "spec.md").write_text("hi")
    ctx = _ctx(tmp_path)
    assert eval_artifact_exists({"path": "spec.md"}, ctx) == MET


def test_artifact_exists_not_found(tmp_path: Path):
    ctx = _ctx(tmp_path)
    assert eval_artifact_exists({"path": "missing.md"}, ctx) == NOT_MET


def test_artifact_exists_glob(tmp_path: Path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "spec.md").write_text("hi")
    ctx = _ctx(tmp_path)
    assert eval_artifact_exists({"path": "docs/*.md"}, ctx) == MET


def test_artifact_exists_no_path(tmp_path: Path):
    assert eval_artifact_exists({}, _ctx(tmp_path)) == UNKNOWN


def test_artifact_absent_when_missing(tmp_path: Path):
    ctx = _ctx(tmp_path)
    assert eval_artifact_absent({"path": "nope.md"}, ctx) == MET


def test_artifact_absent_when_present(tmp_path: Path):
    (tmp_path / "x.md").write_text("hi")
    ctx = _ctx(tmp_path)
    assert eval_artifact_absent({"path": "x.md"}, ctx) == NOT_MET


# ---------------------------------------------------------------------------
# tests_present (stack-aware test gate)
# ---------------------------------------------------------------------------


def test_tests_present_pytest_layout(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_x(): pass\n")
    assert eval_tests_present({}, _ctx(tmp_path)) == MET


def test_tests_present_pytest_suffix_anywhere(tmp_path: Path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "thing_test.py").write_text("def test(): pass\n")
    assert eval_tests_present({}, _ctx(tmp_path)) == MET


def test_tests_present_vitest_when_package_json(tmp_path: Path):
    (tmp_path / "package.json").write_text('{"name": "x"}')
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "date.test.ts").write_text("test('x', () => {})\n")
    assert eval_tests_present({}, _ctx(tmp_path)) == MET


def test_tests_present_js_ignored_without_package_json(tmp_path: Path):
    # A *.test.ts with no package.json isn't a recognized JS/TS project.
    (tmp_path / "a.test.ts").write_text("x")
    assert eval_tests_present({}, _ctx(tmp_path)) == NOT_MET


def test_tests_present_excludes_vendored_dirs(tmp_path: Path):
    (tmp_path / "package.json").write_text("{}")
    nm = tmp_path / "node_modules" / "dep"
    nm.mkdir(parents=True)
    (nm / "bundled.test.js").write_text("x")
    assert eval_tests_present({}, _ctx(tmp_path)) == NOT_MET


def test_tests_present_empty_repo(tmp_path: Path):
    assert eval_tests_present({}, _ctx(tmp_path)) == NOT_MET


def test_tests_present_extra_globs(tmp_path: Path):
    (tmp_path / "pkg_test.go").write_text("package x\n")
    assert eval_tests_present({"extra_globs": ["**/*_test.go"]}, _ctx(tmp_path)) == MET


# ---------------------------------------------------------------------------
# artifact_contains
# ---------------------------------------------------------------------------


def test_artifact_contains_named_sections(tmp_path: Path):
    f = tmp_path / "spec.md"
    f.write_text("## Acceptance Criteria\n\nsome text\n\n## Out of Scope\n\nmore\n")
    ctx = _ctx(tmp_path)
    result = eval_artifact_contains(
        {"path": "spec.md", "sections": ["Acceptance Criteria", "Out of Scope"]},
        ctx,
    )
    assert result == MET


def test_artifact_contains_missing_section(tmp_path: Path):
    f = tmp_path / "spec.md"
    f.write_text("## Acceptance Criteria\n\nonly one section\n")
    ctx = _ctx(tmp_path)
    result = eval_artifact_contains(
        {"path": "spec.md", "sections": ["Acceptance Criteria", "Out of Scope"]},
        ctx,
    )
    assert result == NOT_MET


def test_artifact_contains_section_with_trailing_qualifier(tmp_path: Path):
    # A heading with a trailing qualifier still satisfies the bare section name —
    # the exact-match brittleness that silently blocked phase transitions.
    f = tmp_path / "spec.md"
    f.write_text("## Acceptance Criteria\n\nx\n\n## Out of Scope (this phase)\n\ny\n")
    ctx = _ctx(tmp_path)
    result = eval_artifact_contains(
        {"path": "spec.md", "sections": ["Acceptance Criteria", "Out of Scope"]},
        ctx,
    )
    assert result == MET


def test_artifact_contains_section_case_insensitive(tmp_path: Path):
    f = tmp_path / "spec.md"
    f.write_text("## acceptance criteria\n\nx\n\n## OUT OF SCOPE:\n\ny\n")
    ctx = _ctx(tmp_path)
    result = eval_artifact_contains(
        {"path": "spec.md", "sections": ["Acceptance Criteria", "Out of Scope"]},
        ctx,
    )
    assert result == MET


def test_artifact_contains_section_word_boundary_not_fooled(tmp_path: Path):
    # A heading that merely shares a prefix (no word boundary) must NOT satisfy
    # the section: "Reviewer Notes" does not provide a "Review" section.
    f = tmp_path / "qa.md"
    f.write_text("## Reviewer Notes\n\nx\n")
    ctx = _ctx(tmp_path)
    assert eval_artifact_contains({"path": "qa.md", "sections": ["Review"]}, ctx) == NOT_MET


def test_artifact_contains_pattern(tmp_path: Path):
    f = tmp_path / "code.py"
    f.write_text("def hello():\n    pass\n")
    ctx = _ctx(tmp_path)
    assert eval_artifact_contains({"path": "code.py", "pattern": r"def \w+"}, ctx) == MET
    assert eval_artifact_contains({"path": "code.py", "pattern": r"class \w+"}, ctx) == NOT_MET


def test_artifact_contains_file_missing(tmp_path: Path):
    ctx = _ctx(tmp_path)
    assert eval_artifact_contains({"path": "nope.md", "sections": ["X"]}, ctx) == NOT_MET


def test_artifact_contains_returns_unknown_on_io_error(tmp_path: Path):
    ctx = _ctx(tmp_path)
    with patch("agentalloy.signals.predicates._read_file", return_value=None):
        f = tmp_path / "spec.md"
        f.write_text("hi")
        result = eval_artifact_contains({"path": "spec.md", "sections": ["X"]}, ctx)
    assert result == UNKNOWN


# ---------------------------------------------------------------------------
# artifact_size_min
# ---------------------------------------------------------------------------


def test_artifact_size_min_passes(tmp_path: Path):
    f = tmp_path / "big.md"
    f.write_text("x" * 900)
    ctx = _ctx(tmp_path)
    assert eval_artifact_size_min({"path": "big.md", "bytes": 800}, ctx) == MET


def test_artifact_size_min_fails(tmp_path: Path):
    f = tmp_path / "small.md"
    f.write_text("tiny")
    ctx = _ctx(tmp_path)
    assert eval_artifact_size_min({"path": "small.md", "bytes": 800}, ctx) == NOT_MET


# ---------------------------------------------------------------------------
# artifact_newer_than
# ---------------------------------------------------------------------------


def test_artifact_newer_than(tmp_path: Path):
    import time

    marker = tmp_path / "marker"
    marker.write_text("m")
    time.sleep(0.01)
    artifact = tmp_path / "artifact.md"
    artifact.write_text("a")
    ctx = _ctx(tmp_path)
    assert eval_artifact_newer_than({"path": "artifact.md", "since": "marker"}, ctx) == MET


def test_artifact_newer_than_fails(tmp_path: Path):
    import time

    artifact = tmp_path / "artifact.md"
    artifact.write_text("a")
    time.sleep(0.01)
    marker = tmp_path / "marker"
    marker.write_text("m")
    ctx = _ctx(tmp_path)
    assert eval_artifact_newer_than({"path": "artifact.md", "since": "marker"}, ctx) == NOT_MET


# ---------------------------------------------------------------------------
# phase_in / phase_not_in
# ---------------------------------------------------------------------------


def test_phase_in_met(tmp_path: Path):
    ctx = _ctx(tmp_path, current_phase="build")
    assert eval_phase_in({"phases": ["build", "qa"]}, ctx) == MET


def test_phase_in_not_met(tmp_path: Path):
    ctx = _ctx(tmp_path, current_phase="spec")
    assert eval_phase_in({"phases": ["build", "qa"]}, ctx) == NOT_MET


def test_phase_in_unknown_when_no_phase(tmp_path: Path):
    ctx = _ctx(tmp_path, current_phase=None)
    assert eval_phase_in({"phases": ["build"]}, ctx) == UNKNOWN


def test_phase_not_in(tmp_path: Path):
    ctx = _ctx(tmp_path, current_phase="spec")
    assert eval_phase_not_in({"phases": ["build", "qa"]}, ctx) == MET


# ---------------------------------------------------------------------------
# tool_use predicates
# ---------------------------------------------------------------------------


def test_tool_use_about_to_fire_met(tmp_path: Path):
    ctx = _ctx(tmp_path, recent_tool_use={"tool": "git commit", "path": "", "args": {}})
    assert eval_tool_use_about_to_fire({"tools": ["git commit"]}, ctx) == MET


def test_tool_use_about_to_fire_not_met(tmp_path: Path):
    ctx = _ctx(tmp_path, recent_tool_use={"tool": "Bash", "path": "", "args": {}})
    assert eval_tool_use_about_to_fire({"tools": ["git commit"]}, ctx) == NOT_MET


def test_tool_use_no_context(tmp_path: Path):
    ctx = _ctx(tmp_path, recent_tool_use=None)
    assert eval_tool_use_about_to_fire({"tools": ["git commit"]}, ctx) == UNKNOWN


# ---------------------------------------------------------------------------
# git_state
# ---------------------------------------------------------------------------


def test_git_state_caching(tmp_path: Path):
    """Multiple calls in same eval don't re-shell-out."""
    ctx = _ctx(tmp_path)
    call_count = [0]
    orig = subprocess.run

    def patched_run(*a: Any, **kw: Any) -> Any:
        if "git" in str(a[0]):
            call_count[0] += 1
        return orig(*a, **kw)  # pyright: ignore[reportUnknownVariableType]

    with patch("agentalloy.signals.predicates.subprocess.run", side_effect=patched_run):
        eval_git_state({"has_staged": False}, ctx)
        eval_git_state({"has_uncommitted": False}, ctx)

    # Cached: only one subprocess.run for git status
    assert call_count[0] <= 1


def test_git_state_returns_unknown_on_failure(tmp_path: Path):
    ctx = _ctx(tmp_path)
    with patch("agentalloy.signals.predicates.subprocess.run", side_effect=OSError("no git")):
        result = eval_git_state({"has_staged": True}, ctx)
    assert result == UNKNOWN


# ---------------------------------------------------------------------------
# contract_exists / contract_has_tags
# ---------------------------------------------------------------------------


def test_contract_exists_found(tmp_path: Path):
    cd = tmp_path / ".agentalloy" / "contracts" / "build"
    cd.mkdir(parents=True)
    (cd / "task.md").write_text("---\nphase: build\ntask_slug: t\ndomain_tags: [A]\n---\n\nbody\n")
    ctx = _ctx(tmp_path, contracts_root=tmp_path / ".agentalloy" / "contracts")
    assert eval_contract_exists({"phase": "build", "count_min": 1}, ctx) == MET


def test_contract_exists_not_found(tmp_path: Path):
    ctx = _ctx(tmp_path)
    assert eval_contract_exists({"phase": "build", "count_min": 1}, ctx) == NOT_MET


def test_contract_has_tags(tmp_path: Path):
    import yaml

    cd = tmp_path / ".agentalloy" / "contracts" / "build"
    cd.mkdir(parents=True)
    fm = {"phase": "build", "task_slug": "t", "domain_tags": ["NestJS", "JWT"]}
    (cd / "task.md").write_text(f"---\n{yaml.dump(fm)}---\n\nbody\n")
    ctx = _ctx(tmp_path, contracts_root=tmp_path / ".agentalloy" / "contracts")
    assert eval_contract_has_tags({"phase": "build", "any_of": ["NestJS"]}, ctx) == MET
    assert eval_contract_has_tags({"phase": "build", "any_of": ["React"]}, ctx) == NOT_MET


# ---------------------------------------------------------------------------
# file_type_active
# ---------------------------------------------------------------------------


def test_file_type_active_from_events(tmp_path: Path):
    ctx = _ctx(tmp_path, file_events_since=[Path("src/app.ts")])
    assert eval_file_type_active({"extensions": [".ts"]}, ctx) == MET
    assert eval_file_type_active({"extensions": [".py"]}, ctx) == NOT_MET


def test_file_type_active_no_context(tmp_path: Path):
    ctx = _ctx(tmp_path, file_events_since=[], recent_tool_use=None)
    assert eval_file_type_active({"extensions": [".ts"]}, ctx) == UNKNOWN


# ---------------------------------------------------------------------------
# evaluate_predicate — unknown name raises ValueError
# ---------------------------------------------------------------------------


def test_evaluate_predicate_unknown_name_raises(tmp_path: Path):
    ctx = _ctx(tmp_path)
    with pytest.raises(ValueError, match="Unknown predicate"):
        evaluate_predicate("nonexistent_predicate", {}, ctx)


# ---------------------------------------------------------------------------
# Soft-fail: predicates return UNKNOWN on IO error
# ---------------------------------------------------------------------------


def test_predicate_returns_unknown_on_io_error(tmp_path: Path):
    (tmp_path / "spec.md").write_text("content")
    ctx = _ctx(tmp_path)
    with patch("agentalloy.signals.predicates._read_file", return_value=None):
        result = eval_artifact_contains({"path": "spec.md", "pattern": "x"}, ctx)
    assert result == UNKNOWN


# ---------------------------------------------------------------------------
# section_completeness — banner progress helper
# ---------------------------------------------------------------------------


def test_section_completeness_all_present(tmp_path: Path):
    (tmp_path / "spec.md").write_text("# Title\n## Acceptance Criteria\nx\n## Out of Scope\ny\n")
    present, total, missing = section_completeness(
        "spec.md", ["Acceptance Criteria", "Out of Scope"], tmp_path
    )
    assert (present, total, missing) == (2, 2, [])


def test_section_completeness_some_missing_reports_in_order(tmp_path: Path):
    # Only the second required section is present → present=1, missing keeps decl order.
    (tmp_path / "spec.md").write_text("# Title\n## Out of Scope\ny\n")
    present, total, missing = section_completeness(
        "spec.md", ["Acceptance Criteria", "Out of Scope"], tmp_path
    )
    assert present == 1
    assert total == 2
    assert missing == ["Acceptance Criteria"]


def test_section_completeness_tolerates_trailing_qualifier(tmp_path: Path):
    # `_section_present` matching: a trailing qualifier still satisfies the bare name.
    (tmp_path / "spec.md").write_text("## Acceptance Criteria:\n## Out of Scope (this phase)\n")
    present, total, missing = section_completeness(
        "spec.md", ["Acceptance Criteria", "Out of Scope"], tmp_path
    )
    assert (present, total, missing) == (2, 2, [])


def test_section_completeness_missing_file_returns_all_missing(tmp_path: Path):
    # No file matches the glob → (0, total, all required) by definition; never raises.
    present, total, missing = section_completeness(
        "docs/spec/*.md", ["Acceptance Criteria", "Out of Scope"], tmp_path
    )
    assert present == 0
    assert total == 2
    assert missing == ["Acceptance Criteria", "Out of Scope"]


def test_section_completeness_glob_uses_first_match(tmp_path: Path):
    (tmp_path / "docs" / "spec").mkdir(parents=True)
    (tmp_path / "docs" / "spec" / "a.md").write_text("## Acceptance Criteria\n")
    present, total, missing = section_completeness(
        "docs/spec/*.md", ["Acceptance Criteria", "Out of Scope"], tmp_path
    )
    assert present == 1
    assert total == 2
    assert missing == ["Out of Scope"]


def test_section_completeness_no_required_sections(tmp_path: Path):
    # Empty requirement list → (0, 0, []); the banner caller then appends no progress.
    assert section_completeness("anything.md", [], tmp_path) == (0, 0, [])


def test_section_completeness_unreadable_file_returns_all_missing(tmp_path: Path):
    (tmp_path / "spec.md").write_text("## Acceptance Criteria\n")
    with patch("agentalloy.signals.predicates._read_file", return_value=None):
        present, total, missing = section_completeness("spec.md", ["Acceptance Criteria"], tmp_path)
    assert (present, total, missing) == (0, 1, ["Acceptance Criteria"])


# ---------------------------------------------------------------------------
# approval_recorded (#10 — human-in-the-loop approval gate)
# ---------------------------------------------------------------------------


def _spec_doc(tmp_path: Path) -> Path:
    d = tmp_path / "docs" / "spec"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "x.md"
    f.write_text("# spec\n")
    return f


def _marker(tmp_path: Path, phase: str = "spec") -> Path:
    m = approval_marker_path(tmp_path, phase)
    m.parent.mkdir(parents=True, exist_ok=True)
    m.write_text('approver: u\napproved_at: "2026-01-01T00:00:00Z"\nartifact_sha256: x\n')
    return m


def test_approval_required_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    assert approval_required("spec") is True
    assert approval_required("design") is True
    assert approval_required("build") is False
    assert approval_required(None) is False
    monkeypatch.delenv("SDD_FAST_REQUIRE_APPROVAL", raising=False)
    assert approval_required("sdd-fast") is False
    monkeypatch.setenv("SDD_FAST_REQUIRE_APPROVAL", "1")
    assert approval_required("sdd-fast") is True


def test_approval_required_add_skill_unconditional(monkeypatch: pytest.MonkeyPatch) -> None:
    # add-skill is in _ALWAYS_APPROVAL_PHASES: installing a skill changes what
    # gets composed into every future session — no settings flag can waive it
    # (contrast sdd-fast, which is flag-gated).
    monkeypatch.delenv("SDD_FAST_REQUIRE_APPROVAL", raising=False)
    assert approval_required("add-skill") is True
    monkeypatch.setenv("SDD_FAST_REQUIRE_APPROVAL", "0")
    assert approval_required("add-skill") is True


def test_approval_recorded_no_marker_not_met(tmp_path: Path) -> None:
    _spec_doc(tmp_path)
    ctx = _ctx(tmp_path, current_phase="spec")
    assert eval_approval_recorded({"since": "docs/spec/*.md"}, ctx) == NOT_MET


def test_approval_recorded_marker_postdates_met(tmp_path: Path) -> None:
    doc = _spec_doc(tmp_path)
    marker = _marker(tmp_path)
    future = doc.stat().st_mtime + 10
    os.utime(marker, (future, future))
    ctx = _ctx(tmp_path, current_phase="spec")
    assert eval_approval_recorded({"since": "docs/spec/*.md"}, ctx) == MET


def test_approval_recorded_stale_not_met(tmp_path: Path) -> None:
    doc = _spec_doc(tmp_path)
    marker = _marker(tmp_path)
    # Artifact edited *after* approval → stale → NOT_MET.
    future = marker.stat().st_mtime + 10
    os.utime(doc, (future, future))
    ctx = _ctx(tmp_path, current_phase="spec")
    assert eval_approval_recorded({"since": "docs/spec/*.md"}, ctx) == NOT_MET


def test_approval_recorded_no_phase_unknown(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, current_phase=None)
    assert eval_approval_recorded({"since": "docs/spec/*.md"}, ctx) == UNKNOWN


def test_approval_recorded_route_not_required_met(tmp_path: Path) -> None:
    # build is never approval-gated → MET even with no marker.
    ctx = _ctx(tmp_path, current_phase="build")
    assert eval_approval_recorded({"since": "docs/spec/*.md"}, ctx) == MET


def test_approval_recorded_sdd_fast_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "docs" / "fast").mkdir(parents=True)
    (tmp_path / "docs" / "fast" / "x.md").write_text("# fast\n")
    ctx = _ctx(tmp_path, current_phase="sdd-fast")
    # OFF (default) → fast lane ungated → MET without a marker.
    monkeypatch.delenv("SDD_FAST_REQUIRE_APPROVAL", raising=False)
    assert eval_approval_recorded({"since": "docs/fast/*.md"}, ctx) == MET
    # ON → gated, no marker → NOT_MET.
    monkeypatch.setenv("SDD_FAST_REQUIRE_APPROVAL", "1")
    assert eval_approval_recorded({"since": "docs/fast/*.md"}, ctx) == NOT_MET


def test_approval_recorded_via_registry(tmp_path: Path) -> None:
    # Registered in PREDICATES → reachable through evaluate_predicate.
    _spec_doc(tmp_path)
    ctx = _ctx(tmp_path, current_phase="spec")
    assert evaluate_predicate("approval_recorded", {"since": "docs/spec/*.md"}, ctx) == NOT_MET


# ---------------------------------------------------------------------------
# build_contracts_cover_tasks / build_contract_tag_focus (#12 / #12b)
# ---------------------------------------------------------------------------


def _write_tasks(tmp_path: Path, *, slug: str, items: int) -> None:
    d = tmp_path / "docs" / "design" / slug
    d.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"- task {i}" for i in range(items))
    (d / "tasks.md").write_text(f"# {slug}\n\n## Tasks\n\n{body}\n")


def _write_build_contract(tmp_path: Path, *, name: str, tags: list[str]) -> None:
    bc = tmp_path / ".agentalloy" / "contracts" / "build"
    bc.mkdir(parents=True, exist_ok=True)
    tag_str = "[" + ", ".join(tags) + "]"
    (bc / name).write_text(f"---\nphase: build\ndomain_tags: {tag_str}\n---\n\n# {name}\n")


def test_cover_tasks_met(tmp_path: Path) -> None:
    # 3 tasks, 3 build contracts → covered → MET.
    _write_tasks(tmp_path, slug="feat", items=3)
    for i in range(3):
        _write_build_contract(tmp_path, name=f"0{i}-t.md", tags=["react"])
    assert eval_build_contracts_cover_tasks({}, _ctx(tmp_path)) == MET


def test_cover_tasks_not_met_monolith(tmp_path: Path) -> None:
    # 8 tasks, 1 whole-feature contract → the bug case → NOT_MET.
    _write_tasks(tmp_path, slug="feat", items=8)
    _write_build_contract(tmp_path, name="01-all.md", tags=["react"])
    assert eval_build_contracts_cover_tasks({}, _ctx(tmp_path)) == NOT_MET


def test_cover_tasks_no_tasks_file_unknown(tmp_path: Path) -> None:
    # No tasks.md anywhere → UNKNOWN (the preceding artifact_exists node owns this).
    assert eval_build_contracts_cover_tasks({}, _ctx(tmp_path)) == UNKNOWN


def test_cover_tasks_unparseable_clamps_to_one(tmp_path: Path) -> None:
    # `## Tasks` heading but no list items (0) → floor-clamped to 1; 1 contract → MET.
    d = tmp_path / "docs" / "design" / "feat"
    d.mkdir(parents=True)
    (d / "tasks.md").write_text("# feat\n\n## Tasks\n\nprose only, no list items.\n")
    _write_build_contract(tmp_path, name="01-t.md", tags=["react"])
    assert eval_build_contracts_cover_tasks({}, _ctx(tmp_path)) == MET


def test_tag_focus_met_all_within_two(tmp_path: Path) -> None:
    _write_build_contract(tmp_path, name="01-date.md", tags=["calendar"])
    _write_build_contract(tmp_path, name="02-scaffold.md", tags=["vite", "react"])
    assert eval_build_contract_tag_focus({}, _ctx(tmp_path)) == MET


def test_tag_focus_not_met_names_offender(tmp_path: Path) -> None:
    # A 3-tag contract violates the ≤2 rule → NOT_MET; the advisory (gates.py) names it.
    _write_build_contract(tmp_path, name="01-ok.md", tags=["react"])
    _write_build_contract(tmp_path, name="02-bad.md", tags=["react", "typescript", "vite"])
    assert eval_build_contract_tag_focus({}, _ctx(tmp_path)) == NOT_MET


def test_tag_focus_no_contracts_unknown(tmp_path: Path) -> None:
    assert eval_build_contract_tag_focus({}, _ctx(tmp_path)) == UNKNOWN


def test_new_predicates_registered(tmp_path: Path) -> None:
    _write_tasks(tmp_path, slug="feat", items=1)
    _write_build_contract(tmp_path, name="01-t.md", tags=["react"])
    ctx = _ctx(tmp_path)
    assert evaluate_predicate("build_contracts_cover_tasks", {}, ctx) == MET
    assert evaluate_predicate("build_contract_tag_focus", {}, ctx) == MET
