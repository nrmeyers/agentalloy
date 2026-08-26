"""Per-predicate unit tests for agentalloy.signals.predicates."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from agentalloy.signals.predicates import (
    PredicateContext,
    PredicateResult,
    _glob_files,
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
    eval_scope_touched_in_diff,
    eval_tests_present,
    eval_tool_use_about_to_fire,
    evaluate_predicate,
    section_completeness,
)
from agentalloy.storage.state_store import DuckDBStateStore

MET = PredicateResult.MET
NOT_MET = PredicateResult.NOT_MET
UNKNOWN = PredicateResult.UNKNOWN


def _ctx(tmp_path: Path, **kwargs: Any) -> PredicateContext:
    defaults: dict[str, Any] = dict(project_root=tmp_path, current_phase="build")
    defaults.update(kwargs)
    return PredicateContext(**defaults)


def _make_store(tmp_path: Path) -> DuckDBStateStore:
    """Create a DuckDB store for testing contract predicates."""
    db = tmp_path / "test_state.db"
    store = DuckDBStateStore(db)
    store.open()
    store.migrate()
    return store


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


def _make_store_with_phase_ref(tmp_path: Path, sha: str = "abc123") -> None:
    """A store with a build phase row + phase-entry ref stamped (no contract).

    ``tests_present`` is phase-scoped by the phase-entry ref, not by a contract,
    so this is all the setup the diff-aware path needs.
    """
    store = _make_store(tmp_path)
    _write_phase_start_ref(store, sha)
    store.close()


def test_tests_present_met_when_phase_added_test_file(tmp_path: Path) -> None:
    # The build wrote a test this phase: the diff includes it → MET.
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_x(): pass\n")
    _make_store_with_phase_ref(tmp_path)
    ctx = _ctx(tmp_path, store=_get_store(tmp_path))
    with patch(
        "agentalloy.signals.predicates.subprocess.run",
        side_effect=_git_run_factory(["tests/test_x.py"], []),
    ):
        assert eval_tests_present({}, ctx) == MET


def test_tests_present_not_met_when_tests_preexist_but_phase_touched_none(tmp_path: Path) -> None:
    # F2: tests exist from prior work, but this phase only touched source → NOT_MET.
    # A repo-wide existence glob would have passed this vacuously.
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_old.py").write_text("def test_old(): pass\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text("x = 1\n")
    _make_store_with_phase_ref(tmp_path)
    ctx = _ctx(tmp_path, store=_get_store(tmp_path))
    with patch(
        "agentalloy.signals.predicates.subprocess.run",
        side_effect=_git_run_factory(["src/mod.py"], []),
    ):
        assert eval_tests_present({}, ctx) == NOT_MET


def test_tests_present_met_when_phase_modified_test_file(tmp_path: Path) -> None:
    # Modifying an existing test (working tree) also counts as writing tests.
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_x(): pass\n")
    _make_store_with_phase_ref(tmp_path)
    ctx = _ctx(tmp_path, store=_get_store(tmp_path))
    with patch(
        "agentalloy.signals.predicates.subprocess.run",
        side_effect=_git_run_factory([], ["tests/test_x.py"]),
    ):
        assert eval_tests_present({}, ctx) == MET


def test_tests_present_fail_open_when_no_phase_start_ref(tmp_path: Path) -> None:
    # Store present but no phase-entry ref → can't prove the phase wrote tests;
    # degrade to existence → MET (never NOT_MET on an infra gap).
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_x(): pass\n")
    _make_store(tmp_path).close()
    ctx = _ctx(tmp_path, store=_get_store(tmp_path))
    assert eval_tests_present({}, ctx) == MET


def test_tests_present_fail_open_when_git_fails(tmp_path: Path) -> None:
    # Phase-entry ref present but git fails → infra gap; fail open to existence.
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_x(): pass\n")
    _make_store_with_phase_ref(tmp_path)
    ctx = _ctx(tmp_path, store=_get_store(tmp_path))
    with patch(
        "agentalloy.signals.predicates.subprocess.run",
        side_effect=OSError("no git"),
    ):
        assert eval_tests_present({}, ctx) == MET


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
# #518 / #501 — work-item scoping: a PRIOR item's artifact must not affect the
# current item's gate verdict (the shared symptom of #378/#501, made the
# default for store-backed artifact queries).
# ---------------------------------------------------------------------------


def _seed_spec_items(
    tmp_path: Path, *, cursor_slug: str | None = "slug-b", slug_a_spec: str | None = None
) -> PredicateContext:
    """Two spec work-items (slug-a, slug-b) with store artifacts + contracts.

    ``slug_a_spec`` controls whether slug-a records a ``spec.md`` artifact: when
    omitted it has none (for the ``artifact_exists`` scoping case); to exercise
    ``artifact_contains`` leave it as the INCOMPLETE spec. The cursor (stored in
    the store, not on disk — #514) pins the selected item, so the gate must
    judge only that item's artifacts.
    """
    store = DuckDBStateStore(tmp_path / "test_state.db")
    store.open()
    store.migrate()
    repo = store._repo()  # type: ignore[attr-defined]
    rows = [
        f"('{repo}', 'spec/slug-a', 'slug-a', '[]', NULL, 'spec', 'active', CURRENT_TIMESTAMP)",
        f"('{repo}', 'spec/slug-b', 'slug-b', '[]', NULL, 'spec', 'active', CURRENT_TIMESTAMP)",
    ]
    store.execute(
        "INSERT INTO sdd_contract "
        "(repo, contract_id, slug, domain_tags, work_item, phase, status, updated_at) "
        "VALUES " + ", ".join(rows) + ";"
    )
    if slug_a_spec is not None:
        store.set_artifact("spec", "slug-a", "spec.md", slug_a_spec)
    store.set_artifact(
        "spec",
        "slug-b",
        "spec.md",
        "# Complete\n\n## Acceptance Criteria\n- a\n\n## Out of Scope\n- b\n",
    )
    if cursor_slug is not None:
        # Store-backed cursor (#514) — no .agentalloy/cursor file on disk.
        store.write("cursor", f"active/spec/{cursor_slug}.md")
    return _ctx(tmp_path, current_phase="spec", store=store)


def test_regression_prior_item_artifact_does_not_poison_contains(tmp_path: Path) -> None:
    """#501/#518: slug-a's incomplete spec must NOT block slug-b's exit.

    The gate scopes to the cursor'd work item (slug-b), whose spec carries both
    required sections → MET, despite slug-a's spec lacking them repo-wide.
    """
    ctx = _seed_spec_items(
        tmp_path, cursor_slug="slug-b", slug_a_spec="# Incomplete\n\nno sections here\n"
    )
    result = eval_artifact_contains(
        {
            "phase": "spec",
            "name": "*.md",
            "sections": ["Acceptance Criteria", "Out of Scope"],
        },
        ctx,
    )
    assert result == MET


def test_regression_artifact_exists_is_slug_scoped(tmp_path: Path) -> None:
    """A prior item's artifact must not satisfy THIS item's artifact_exists.

    Recurrences of the defect: with slug-a cursor'd (no ``spec.md`` artifact for
    it) the gate must be NOT_MET even though slug-b's ``spec.md`` exists — the
    repo-global query that leaked prior items is what broke it (#501).
    """
    store = DuckDBStateStore(tmp_path / "test_state.db")
    store.open()
    store.migrate()
    repo = store._repo()  # type: ignore[attr-defined]
    store.execute(
        "INSERT INTO sdd_contract "
        "(repo, contract_id, slug, domain_tags, work_item, phase, status, updated_at) "
        f"VALUES ('{repo}','spec/slug-a','slug-a','[]',NULL,'spec','active',CURRENT_TIMESTAMP), "
        f"('{repo}','spec/slug-b','slug-b','[]',NULL,'spec','active',CURRENT_TIMESTAMP);"
    )
    # slug-a has NO spec.md artifact; slug-b does.
    store.set_artifact("spec", "slug-b", "spec.md", "# Complete\n")

    # Cursor pins slug-a → its gate must be NOT_MET (slug-b's spec.md not counted).
    store.write("cursor", "active/spec/slug-a.md")
    ctx_a = _ctx(tmp_path, current_phase="spec", store=store)
    assert eval_artifact_exists({"phase": "spec", "name": "spec.md"}, ctx_a) == NOT_MET

    # Repoint the cursor to slug-b → its own spec.md counts → MET.
    store.write("cursor", "active/spec/slug-b.md")
    ctx_b = _ctx(tmp_path, current_phase="spec", store=store)
    assert eval_artifact_exists({"phase": "spec", "name": "spec.md"}, ctx_b) == MET


def test_active_slug_resolves_from_store_cursor(tmp_path: Path) -> None:
    """#514: the active work-item slug resolves from the STORE cursor, with no
    ``.agentalloy/cursor`` file on disk (that disk read is #514's migration
    target — the store is authoritative)."""
    from agentalloy.signals.predicates import _resolve_workitem_slug  # noqa: PLC0415

    ctx = _seed_spec_items(tmp_path, cursor_slug="slug-b")
    assert not (tmp_path / ".agentalloy" / "cursor").exists()
    assert ctx.resolve_active_slug("spec") == "slug-b"
    # The old helper is the same single resolution point.
    assert _resolve_workitem_slug(ctx, "spec") == "slug-b"


def test_single_contract_falls_back_to_sole_slug(tmp_path: Path) -> None:
    """No cursor, single active contract in the phase → that slug is the scope."""
    store = DuckDBStateStore(tmp_path / "test_state.db")
    store.open()
    store.migrate()
    repo = store._repo()  # type: ignore[attr-defined]
    store.execute(
        "INSERT INTO sdd_contract "
        "(repo, contract_id, slug, domain_tags, work_item, phase, status, updated_at) "
        f"VALUES ('{repo}', 'spec/only', 'only', '[]', NULL, 'spec', 'active', CURRENT_TIMESTAMP);"
    )
    store.set_artifact("spec", "only", "spec.md", "# Only\n")
    ctx = _ctx(tmp_path, current_phase="spec", store=store)
    assert ctx.resolve_active_slug("spec") == "only"


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
    store = _make_store(tmp_path)
    repo = store._repo()  # type: ignore[attr-defined]
    store.execute(
        f"""INSERT INTO sdd_contract (repo, contract_id, slug, domain_tags, work_item, phase, status, updated_at)
        VALUES ('{repo}', 'c-001', 'task', '["A"]', NULL, 'build', 'active', CURRENT_TIMESTAMP)"""
    )
    store.close()
    ctx = _ctx(tmp_path, store=_get_store(tmp_path))
    assert eval_contract_exists({"phase": "build", "count_min": 1}, ctx) == MET


def test_contract_exists_not_found(tmp_path: Path):
    ctx = _ctx(tmp_path)
    assert eval_contract_exists({"phase": "build", "count_min": 1}, ctx) == NOT_MET


def test_contract_has_tags(tmp_path: Path):
    store = _make_store(tmp_path)
    repo = store._repo()  # type: ignore[attr-defined]
    store.execute(
        f"""INSERT INTO sdd_contract (repo, contract_id, slug, domain_tags, work_item, phase, status, updated_at)
        VALUES ('{repo}', 'c-001', 'task', '["NestJS","JWT"]', NULL, 'build', 'active', CURRENT_TIMESTAMP)"""
    )
    store.close()
    ctx = _ctx(tmp_path, store=_get_store(tmp_path))
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


def _get_store(tmp_path: Path) -> DuckDBStateStore | None:
    """Get the store attached to tmp_path, if any."""
    db = tmp_path / "test_state.db"
    if db.exists():
        return DuckDBStateStore(db, read_only=True).open()
    return None


def _seed_design(tmp_path: Path, slug: str) -> None:
    """A design-phase contract so the gate resolves ``slug`` as the work-item.

    Writes to both the store (authoritative) and filesystem (for cursor resolution).
    The contract slug matches the work_item so _resolve_workitem_slug -> _item_build_contracts works.
    Creates the store if it doesn't exist yet.
    """
    # Filesystem: for cursor file
    d = tmp_path / ".agentalloy" / "contracts" / "active" / "design"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{slug}.md").write_text(f"---\nphase: design\ntask_slug: {slug}\n---\n\n# {slug}\n")

    # Store: authoritative source (create if needed)
    db = tmp_path / "test_state.db"
    if not db.exists():
        _make_store(tmp_path).close()
    store = DuckDBStateStore(db)
    store.open()
    repo = store._repo()  # type: ignore[attr-defined]
    store.execute(
        f"""INSERT INTO sdd_contract (repo, contract_id, slug, domain_tags, work_item, phase, status, updated_at)
        VALUES ('{repo}', 'design-{slug}', '{slug}', '[]', NULL, 'design', 'active', CURRENT_TIMESTAMP)"""
    )
    store.close()


def _set_cursor(tmp_path: Path, rel: str) -> None:
    (tmp_path / ".agentalloy").mkdir(parents=True, exist_ok=True)
    # Store just the slug (stem of the filename) for cursor resolution
    slug = Path(rel).stem
    (tmp_path / ".agentalloy" / "cursor").write_text(slug)


def _write_build_contract(
    tmp_path: Path, *, name: str, tags: list[str], work_item: str | None = "feat"
) -> None:
    """Write a build contract to both filesystem and store.

    The store is authoritative; filesystem is for backward compat.
    """
    # Filesystem: for backward compat
    bc = tmp_path / ".agentalloy" / "contracts" / "active" / "build"
    bc.mkdir(parents=True, exist_ok=True)
    tag_str = "[" + ", ".join(tags) + "]"
    wi = f"work_item: {work_item}\n" if work_item is not None else ""
    (bc / name).write_text(f"---\nphase: build\n{wi}domain_tags: {tag_str}\n---\n\n# {name}\n")

    # Store: authoritative source
    db = tmp_path / "test_state.db"
    if db.exists():
        store = DuckDBStateStore(db)
        store.open()
        repo = store._repo()  # type: ignore[attr-defined]
        slug = Path(name).stem
        tags_json = "[" + ", ".join(f'"{t}"' for t in tags) + "]"
        wi_val = f"'{work_item}'" if work_item is not None else "NULL"
        store.execute(
            f"""INSERT INTO sdd_contract (repo, contract_id, slug, domain_tags, work_item, phase, status, updated_at)
            VALUES ('{repo}', 'build-{slug}', '{slug}', '{tags_json}', {wi_val}, 'build', 'active', CURRENT_TIMESTAMP)"""
        )
        store.close()


# The gate scopes to the cursor'd DESIGN work-item; every case seeds one.
def _design_ctx(tmp_path: Path) -> PredicateContext:
    store = _get_store(tmp_path)
    return _ctx(tmp_path, current_phase="design", store=store)


def test_cover_tasks_met(tmp_path: Path) -> None:
    # 3 tasks, 3 of this item's build contracts → covered → MET.
    _seed_design(tmp_path, "feat")
    _write_tasks(tmp_path, slug="feat", items=3)
    for i in range(3):
        _write_build_contract(tmp_path, name=f"0{i}-t.md", tags=["react"], work_item="feat")
    assert eval_build_contracts_cover_tasks({}, _design_ctx(tmp_path)) == MET


def test_cover_tasks_not_met_monolith(tmp_path: Path) -> None:
    # 8 tasks, 1 whole-feature contract → the bug case → NOT_MET.
    _seed_design(tmp_path, "feat")
    _write_tasks(tmp_path, slug="feat", items=8)
    _write_build_contract(tmp_path, name="01-all.md", tags=["react"], work_item="feat")
    assert eval_build_contracts_cover_tasks({}, _design_ctx(tmp_path)) == NOT_MET


def test_cover_tasks_no_tasks_file_unknown(tmp_path: Path) -> None:
    # Item resolves but no tasks.md → UNKNOWN (a preceding artifact_exists node owns this).
    _seed_design(tmp_path, "feat")
    assert eval_build_contracts_cover_tasks({}, _design_ctx(tmp_path)) == UNKNOWN


def test_cover_tasks_unresolved_workitem_unknown(tmp_path: Path) -> None:
    # No single design work-item resolves (no design contract) → UNKNOWN (fail-open).
    _write_tasks(tmp_path, slug="feat", items=3)
    _write_build_contract(tmp_path, name="01-t.md", tags=["react"], work_item="feat")
    assert eval_build_contracts_cover_tasks({}, _design_ctx(tmp_path)) == UNKNOWN


def test_cover_tasks_unparseable_clamps_to_one(tmp_path: Path) -> None:
    # `## Tasks` heading but no list items (0) → floor-clamped to 1; 1 contract → MET.
    _seed_design(tmp_path, "feat")
    d = tmp_path / "docs" / "design" / "feat"
    d.mkdir(parents=True)
    (d / "tasks.md").write_text("# feat\n\n## Tasks\n\nprose only, no list items.\n")
    _write_build_contract(tmp_path, name="01-t.md", tags=["react"], work_item="feat")
    assert eval_build_contracts_cover_tasks({}, _design_ctx(tmp_path)) == MET


def test_cover_tasks_cursor_scoped_ignores_siblings(tmp_path: Path) -> None:
    # #378 regression: two in-flight design items. `feat` is fully decomposed
    # (3 tasks, 3 own contracts); sibling `sib` has 9 tasks and 0 contracts.
    # The gate judges only the cursor'd `feat` → MET, not blocked by sib's 9 tasks.
    _seed_design(tmp_path, "feat")
    _seed_design(tmp_path, "sib")
    _set_cursor(tmp_path, "active/design/feat.md")
    _write_tasks(tmp_path, slug="feat", items=3)
    _write_tasks(tmp_path, slug="sib", items=9)
    for i in range(3):
        _write_build_contract(tmp_path, name=f"0{i}-f.md", tags=["react"], work_item="feat")
    assert eval_build_contracts_cover_tasks({}, _design_ctx(tmp_path)) == MET


def test_cover_tasks_fallback_repo_global_when_untagged(tmp_path: Path) -> None:
    # Migration bridge: build contracts predate `work_item:` → attribution falls
    # back to counting all of them (old repo-global behavior), so a pre-field repo
    # is neither spuriously blocked nor cross-item masked beyond the legacy norm.
    _seed_design(tmp_path, "feat")
    _write_tasks(tmp_path, slug="feat", items=3)
    for i in range(3):
        _write_build_contract(tmp_path, name=f"0{i}-t.md", tags=["react"], work_item=None)
    assert eval_build_contracts_cover_tasks({}, _design_ctx(tmp_path)) == MET


def test_cover_tasks_tagged_siblings_dont_count(tmp_path: Path) -> None:
    # Once ANY contract is tagged, untagged/other-item ones don't count for `feat`:
    # feat has 3 tasks but only 1 of its own contract (+2 stamped to `sib`) → NOT_MET.
    _seed_design(tmp_path, "feat")
    _write_tasks(tmp_path, slug="feat", items=3)
    _write_build_contract(tmp_path, name="01-f.md", tags=["react"], work_item="feat")
    _write_build_contract(tmp_path, name="02-s.md", tags=["react"], work_item="sib")
    _write_build_contract(tmp_path, name="03-s.md", tags=["react"], work_item="sib")
    assert eval_build_contracts_cover_tasks({}, _design_ctx(tmp_path)) == NOT_MET


def test_tag_focus_met_all_within_two(tmp_path: Path) -> None:
    _seed_design(tmp_path, "feat")
    _write_build_contract(tmp_path, name="01-date.md", tags=["calendar"], work_item="feat")
    _write_build_contract(tmp_path, name="02-scaffold.md", tags=["vite", "react"], work_item="feat")
    assert eval_build_contract_tag_focus({}, _design_ctx(tmp_path)) == MET


def test_tag_focus_not_met_names_offender(tmp_path: Path) -> None:
    # A 3-tag contract violates the ≤2 rule → NOT_MET; the advisory (gates.py) names it.
    _seed_design(tmp_path, "feat")
    _write_build_contract(tmp_path, name="01-ok.md", tags=["react"], work_item="feat")
    _write_build_contract(
        tmp_path, name="02-bad.md", tags=["react", "typescript", "vite"], work_item="feat"
    )
    assert eval_build_contract_tag_focus({}, _design_ctx(tmp_path)) == NOT_MET


def test_tag_focus_cursor_scoped_ignores_sibling_bad_contract(tmp_path: Path) -> None:
    # #378: a SIBLING item's wide-tag contract must not block THIS item's exit.
    _seed_design(tmp_path, "feat")
    _seed_design(tmp_path, "sib")
    _set_cursor(tmp_path, "active/design/feat.md")
    _write_build_contract(tmp_path, name="01-f.md", tags=["react"], work_item="feat")
    _write_build_contract(
        tmp_path, name="02-s.md", tags=["a", "b", "c"], work_item="sib"
    )  # sibling's offender
    assert eval_build_contract_tag_focus({}, _design_ctx(tmp_path)) == MET


def test_tag_focus_no_contracts_unknown(tmp_path: Path) -> None:
    _seed_design(tmp_path, "feat")
    assert eval_build_contract_tag_focus({}, _design_ctx(tmp_path)) == UNKNOWN


def test_new_predicates_registered(tmp_path: Path) -> None:
    _seed_design(tmp_path, "feat")
    _write_tasks(tmp_path, slug="feat", items=1)
    _write_build_contract(tmp_path, name="01-t.md", tags=["react"], work_item="feat")
    ctx = _design_ctx(tmp_path)
    assert evaluate_predicate("build_contracts_cover_tasks", {}, ctx) == MET
    assert evaluate_predicate("build_contract_tag_focus", {}, ctx) == MET


# ---------------------------------------------------------------------------
# scope_touched_in_diff  (build → qa gate: #513)
# ---------------------------------------------------------------------------


def _write_phase_start_ref(store: DuckDBStateStore, sha: str, phase: str = "build") -> None:
    """Stamp the phase-entry ref marker the way ``_record_phase_start_ref`` does.

    The stamp lives in the store's ``phase_start_ref`` blob field, not on disk.
    Requires an existing phase row — writes ``phase`` if none exists yet.
    """
    store.write_phase(phase)
    repo = store.for_repo(store._repo())  # type: ignore[attr-defined]
    repo.set_phase_start_ref(sha)


class _GitResult:
    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout


def _git_run_factory(diff_paths: list[str], status_paths: list[str]):
    """A fake subprocess.run for predicates: returns staged `diff_paths` and dirty `status_paths`."""

    def fake(args: Any, **kw: Any) -> Any:
        argv = args if isinstance(args, list) else [args]
        if "diff" in argv:
            return _GitResult(0, "\n".join(diff_paths) + ("\n" if diff_paths else ""))
        if "status" in argv:
            return _GitResult(0, "".join(f" M {p}\n" for p in status_paths))
        return _GitResult(0, "")

    return fake


def _seed_build_contract(
    tmp_path: Path, *, slug: str = "task-foo", scope_touches: list[str] | None = None
) -> DuckDBStateStore:
    """Create a store with a build-phase contract. Returns the open store for further setup."""
    store = _make_store(tmp_path)
    repo = store._repo()  # type: ignore[attr-defined]
    touches_json = json.dumps(scope_touches if scope_touches is not None else [])
    store.execute(
        f"""INSERT INTO sdd_contract
            (repo, contract_id, slug, domain_tags, work_item, phase, status, scope_touches, scope_avoids, updated_at)
            VALUES ('{repo}', 'c-001', '{slug}', '[]', NULL, 'build', 'active', '{touches_json}', '[]', CURRENT_TIMESTAMP)"""
    )
    return store


def test_path_in_scope_matches_prefix_glob_and_exact() -> None:
    from agentalloy.signals.predicates import _path_in_scope

    assert _path_in_scope("src/api/handler.py", ["src/api/**"])
    assert _path_in_scope("src/auth/user.py", ["src/auth"])  # directory prefix
    assert _path_in_scope("src/auth/user.py", ["src/auth/"])  # trailing slash ok
    assert _path_in_scope("src/router.py", ["src/router.py"])  # exact path
    assert not _path_in_scope("tests/x.py", ["src/api/**"])
    assert not _path_in_scope("src/api/handler.py", ["src/web/**"])


def test_scope_touched_in_diff_unknown_when_no_phase_start_marker(tmp_path: Path) -> None:
    _seed_build_contract(tmp_path)  # has a contract, but no phase-start marker
    ctx = _ctx(tmp_path, store=_get_store(tmp_path))
    assert eval_scope_touched_in_diff({}, ctx) == UNKNOWN


def test_scope_touched_in_diff_met_when_scope_touched(tmp_path: Path) -> None:
    store = _seed_build_contract(tmp_path, scope_touches=["src/api/**"])
    _write_phase_start_ref(store, "abc123")
    store.close()
    ctx = _ctx(tmp_path, store=_get_store(tmp_path))
    with patch(
        "agentalloy.signals.predicates.subprocess.run",
        side_effect=_git_run_factory(["src/api/handler.py"], []),
    ):
        assert eval_scope_touched_in_diff({}, ctx) == MET


def test_scope_touched_in_diff_not_met_when_changes_outside_scope(tmp_path: Path) -> None:
    store = _seed_build_contract(tmp_path, scope_touches=["src/api/**"])
    _write_phase_start_ref(store, "abc123")
    store.close()
    ctx = _ctx(tmp_path, store=_get_store(tmp_path))
    with patch(
        "agentalloy.signals.predicates.subprocess.run",
        side_effect=_git_run_factory(["tests/test_handler.py"], []),
    ):
        assert eval_scope_touched_in_diff({}, ctx) == NOT_MET


def test_scope_touched_in_diff_not_met_when_no_changes(tmp_path: Path) -> None:
    store = _seed_build_contract(tmp_path, scope_touches=["src/api/**"])
    _write_phase_start_ref(store, "abc123")
    store.close()
    ctx = _ctx(tmp_path, store=_get_store(tmp_path))
    with patch(
        "agentalloy.signals.predicates.subprocess.run",
        side_effect=_git_run_factory([], []),
    ):
        assert eval_scope_touched_in_diff({}, ctx) == NOT_MET


def test_scope_touched_in_diff_unknown_when_git_fails(tmp_path: Path) -> None:
    store = _seed_build_contract(tmp_path, scope_touches=["src/api/**"])
    _write_phase_start_ref(store, "abc123")
    store.close()
    ctx = _ctx(tmp_path, store=_get_store(tmp_path))
    with patch(
        "agentalloy.signals.predicates.subprocess.run",
        side_effect=OSError("no git"),
    ):
        assert eval_scope_touched_in_diff({}, ctx) == UNKNOWN


def test_scope_touched_in_diff_unknown_when_no_contract(tmp_path: Path) -> None:
    # Store present but no active build contract → can't scope.
    _make_store(tmp_path).close()
    ctx = _ctx(tmp_path, store=_get_store(tmp_path))
    assert eval_scope_touched_in_diff({}, ctx) == UNKNOWN


def test_scope_touched_in_diff_unknown_when_scope_undeclared(tmp_path: Path) -> None:
    store = _seed_build_contract(tmp_path, scope_touches=[])
    _write_phase_start_ref(store, "abc123")
    store.close()
    ctx = _ctx(tmp_path, store=_get_store(tmp_path))
    assert eval_scope_touched_in_diff({}, ctx) == UNKNOWN


def test_scope_touched_in_diff_unknown_when_no_phase(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, current_phase=None)
    assert eval_scope_touched_in_diff({}, ctx) == UNKNOWN


def test_is_test_path_pytest_layouts() -> None:
    from agentalloy.signals.predicates import _is_test_path, _test_file_patterns

    # No package.json → pytest layouts only.
    patterns = _test_file_patterns(Path("/nonexistent"))
    assert _is_test_path("tests/test_foo.py", patterns)
    assert _is_test_path("tests/api/test_foo.py", patterns)
    assert _is_test_path("tests/foo_test.py", patterns)
    assert _is_test_path("test_foo.py", patterns)  # root-level
    assert _is_test_path("src/foo_test.py", patterns)
    assert _is_test_path("tests/utils.py", patterns)  # any .py under tests/
    assert not _is_test_path("src/api/handler.py", patterns)
    assert not _is_test_path("tests/README.md", patterns)


def test_is_test_path_js_layouts(tmp_path: Path) -> None:
    from agentalloy.signals.predicates import _is_test_path, _test_file_patterns

    (tmp_path / "package.json").write_text("{}")
    patterns = _test_file_patterns(tmp_path)
    assert _is_test_path("src/api/handler.test.ts", patterns)
    assert _is_test_path("handler.spec.tsx", patterns)
    assert _is_test_path("foo.mtest.mts", patterns)
    assert not _is_test_path("src/api/handler.ts", patterns)


def test_scope_touched_in_diff_not_met_when_only_tests_in_scope(tmp_path: Path) -> None:
    # Scope is broad enough to match test files; the build only wrote tests for
    # an out-of-scope plan. Test files must not satisfy the scope gate.
    store = _seed_build_contract(tmp_path, scope_touches=["src/**", "tests/**"])
    _write_phase_start_ref(store, "abc123")
    store.close()
    ctx = _ctx(tmp_path, store=_get_store(tmp_path))
    with patch(
        "agentalloy.signals.predicates.subprocess.run",
        side_effect=_git_run_factory(["tests/test_handler.py"], []),
    ):
        assert eval_scope_touched_in_diff({}, ctx) == NOT_MET


def test_scope_touched_in_diff_met_when_source_and_tests_in_scope(tmp_path: Path) -> None:
    # A real in-scope source file satisfies the gate even when test files are
    # also changed (tests are ignored, source is what proves the scope).
    store = _seed_build_contract(tmp_path, scope_touches=["src/**", "tests/**"])
    _write_phase_start_ref(store, "abc123")
    store.close()
    ctx = _ctx(tmp_path, store=_get_store(tmp_path))
    with patch(
        "agentalloy.signals.predicates.subprocess.run",
        side_effect=_git_run_factory(["tests/test_handler.py", "src/api/handler.py"], []),
    ):
        assert eval_scope_touched_in_diff({}, ctx) == MET


def test_scope_touched_in_diff_not_met_when_whole_repo_scope_only_tests(
    tmp_path: Path,
) -> None:
    # Even a whole-repo scope must not let a tests-only change pass.
    store = _seed_build_contract(tmp_path, scope_touches=["**"])
    _write_phase_start_ref(store, "abc123")
    store.close()
    ctx = _ctx(tmp_path, store=_get_store(tmp_path))
    with patch(
        "agentalloy.signals.predicates.subprocess.run",
        side_effect=_git_run_factory(["tests/test_handler.py"], []),
    ):
        assert eval_scope_touched_in_diff({}, ctx) == NOT_MET


# ---------------------------------------------------------------------------
# _glob_files / artifact_exists directory regression (#513)
# ---------------------------------------------------------------------------


def test_glob_files_returns_only_files(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("x")
    (tmp_path / "src" / "sub").mkdir()  # a subdirectory, no files under it
    matched = _glob_files(tmp_path, "src/**/*")
    # The ``src/sub`` directory is excluded; only the real file remains.
    assert matched == [tmp_path / "src" / "main.py"]
    assert all(f.is_file() for f in matched)


def test_artifact_exists_excludes_empty_directory(tmp_path: Path) -> None:
    """#513: a bare, empty ``src/`` dir must NOT satisfy artifact_exists{path: src/**}."""
    (tmp_path / "src").mkdir()
    assert eval_artifact_exists({"path": "src/**"}, _ctx(tmp_path)) == NOT_MET
