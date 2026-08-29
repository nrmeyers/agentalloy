"""Unit tests for the bootstrap CLI.

All tests use a tmp_path DuckDB skill store so no live Ollama is needed.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from agentalloy.bootstrap import EXIT_OK, EXIT_USAGE, EXIT_VALIDATION, main
from agentalloy.storage.overgraph_skill_store import OverGraphSkillStore, open_overgraph_skill_store

_SAMPLE_MD = textwrap.dedent("""\
    # Sample Governance Rule

    **skill_id:** sys-sample
    **category:** governance
    **always_apply:** true
    **author:** test
    **change_summary:** unit test load

    Never do anything destructive without explicit authorization.
""")

_PHASE_MD = textwrap.dedent("""\
    # Build Phase Rule

    **skill_id:** sys-build-rule
    **category:** governance
    **always_apply:** false
    **phase_scope:** build
    **category_scope:**
    **author:** test
    **change_summary:** phase scoped

    Write tests before implementation.
""")


@pytest.fixture
def md_file(tmp_path: Path) -> Path:
    p = tmp_path / "sys-sample.md"
    p.write_text(_SAMPLE_MD)
    return p


@pytest.fixture
def seeded_db(tmp_path: Path) -> OverGraphSkillStore:
    return open_overgraph_skill_store(str(tmp_path / "agentalloy.overgraph"))


def _make_settings(db_path: str) -> object:
    class FakeSettings:
        corpus_store_path = db_path

    return FakeSettings()


def test_insert_new_skill(tmp_path: Path, md_file: Path) -> None:
    db_path = str(tmp_path / "agentalloy.overgraph")
    store = open_overgraph_skill_store(db_path)  # opens + migrates
    store.close()

    with patch("agentalloy.bootstrap.get_settings", return_value=_make_settings(db_path)):
        code = main([str(md_file), "--yes"])

    assert code == EXIT_OK

    ro = open_overgraph_skill_store(db_path, read_only=True)
    skill = ro.get_skill("sys-sample")
    assert skill is not None
    assert skill.canonical_name == "Sample Governance Rule"
    assert len(ro.get_versions_by_skill("sys-sample")) == 1
    assert len(ro.get_active_fragments_for_skill("sys-sample")) == 1
    assert skill.current_version_id == "sys-sample-v1"
    ro.close()


def test_init_schema_flag(tmp_path: Path, md_file: Path) -> None:
    db_path = str(tmp_path / "agentalloy_new.overgraph")
    # Store doesn't exist yet — --init-schema must create it first
    with patch("agentalloy.bootstrap.get_settings", return_value=_make_settings(db_path)):
        code = main([str(md_file), "--init-schema", "--yes"])

    assert code == EXIT_OK

    store = open_overgraph_skill_store(db_path, read_only=True)
    assert store.count_skills() == 1
    store.close()


def test_duplicate_without_force_fails(tmp_path: Path, md_file: Path) -> None:
    db_path = str(tmp_path / "agentalloy.overgraph")
    store = open_overgraph_skill_store(db_path)  # opens + migrates
    store.close()

    with patch("agentalloy.bootstrap.get_settings", return_value=_make_settings(db_path)):
        main([str(md_file), "--yes"])
        code = main([str(md_file), "--yes"])

    assert code == EXIT_VALIDATION


def test_force_overwrites(tmp_path: Path, md_file: Path) -> None:
    db_path = str(tmp_path / "agentalloy.overgraph")
    store = open_overgraph_skill_store(db_path)  # opens + migrates
    store.close()

    with patch("agentalloy.bootstrap.get_settings", return_value=_make_settings(db_path)):
        main([str(md_file), "--yes"])
        code = main([str(md_file), "--force", "--yes"])

    assert code == EXIT_OK

    ro = open_overgraph_skill_store(db_path, read_only=True)
    assert ro.get_skill("sys-sample") is not None
    ro.close()


def test_file_not_found_returns_usage_error() -> None:
    code = main(["/nonexistent/skill.md", "--yes"])
    assert code == EXIT_USAGE


def test_invalid_markdown_returns_validation_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.md"
    bad.write_text("no heading here\n\n**skill_id:** sys-x\n")
    code = main([str(bad), "--yes"])
    assert code == EXIT_VALIDATION


def test_non_sys_prefix_returns_validation_error(tmp_path: Path) -> None:
    db_path = str(tmp_path / "agentalloy.overgraph")
    store = open_overgraph_skill_store(db_path)  # opens + migrates
    store.close()

    bad_id = tmp_path / "bad_id.md"
    bad_id.write_text(
        textwrap.dedent("""\
        # Domain Skill

        **skill_id:** domain-skill
        **category:** python

        Content.
    """)
    )

    with patch("agentalloy.bootstrap.get_settings", return_value=_make_settings(db_path)):
        code = main([str(bad_id), "--yes"])

    assert code == EXIT_VALIDATION


def test_phase_scoped_skill_inserted(tmp_path: Path) -> None:
    db_path = str(tmp_path / "agentalloy.overgraph")
    store = open_overgraph_skill_store(db_path)  # opens + migrates
    store.close()

    md = tmp_path / "phase.md"
    md.write_text(_PHASE_MD)

    with patch("agentalloy.bootstrap.get_settings", return_value=_make_settings(db_path)):
        code = main([str(md), "--yes"])

    assert code == EXIT_OK

    ro = open_overgraph_skill_store(db_path, read_only=True)
    skill = ro.get_skill("sys-build-rule")
    assert skill is not None
    assert skill.always_apply is False
    assert skill.phase_scope is not None and "build" in skill.phase_scope
    ro.close()


def test_sdd_fast_phase_scope_is_valid(tmp_path: Path) -> None:
    """sys skills can scope to the fast-lane phase — `sdd-fast` is in the
    canonical lifecycle vocabulary, not rejected as unknown."""
    db_path = str(tmp_path / "agentalloy.overgraph")
    store = open_overgraph_skill_store(db_path)  # opens + migrates
    store.close()

    md = tmp_path / "fast.md"
    md.write_text(
        textwrap.dedent("""\
        # Fast Lane Rule

        **skill_id:** sys-fast-rule
        **category:** governance
        **always_apply:** false
        **phase_scope:** sdd-fast
        **category_scope:**
        **author:** test
        **change_summary:** scoped to the fast lane

        Keep the rigor; drop the ceremony.
    """)
    )

    with patch("agentalloy.bootstrap.get_settings", return_value=_make_settings(db_path)):
        code = main([str(md), "--yes"])

    assert code == EXIT_OK
    ro = open_overgraph_skill_store(db_path, read_only=True)
    skill = ro.get_skill("sys-fast-rule")
    assert skill is not None and skill.phase_scope is not None
    assert "sdd-fast" in skill.phase_scope
    ro.close()


def test_always_apply_with_phase_scope_is_validation_error(tmp_path: Path) -> None:
    db_path = str(tmp_path / "agentalloy.overgraph")
    store = open_overgraph_skill_store(db_path)  # opens + migrates
    store.close()

    bad = tmp_path / "conflict.md"
    bad.write_text(
        textwrap.dedent("""\
        # Conflicting Applicability

        **skill_id:** sys-conflict
        **category:** governance
        **always_apply:** true
        **phase_scope:** design

        Content.
    """)
    )

    with patch("agentalloy.bootstrap.get_settings", return_value=_make_settings(db_path)):
        code = main([str(bad), "--yes"])

    assert code == EXIT_VALIDATION


def test_canonical_name_collision_without_force_fails(tmp_path: Path) -> None:
    db_path = str(tmp_path / "agentalloy.overgraph")
    store = open_overgraph_skill_store(db_path)  # opens + migrates
    store.close()

    skill_a = tmp_path / "skill_a.md"
    skill_a.write_text(
        textwrap.dedent("""\
        # Shared Canonical Name

        **skill_id:** sys-skill-a
        **category:** governance
        **always_apply:** true

        Content for skill A.
    """)
    )
    skill_b = tmp_path / "skill_b.md"
    skill_b.write_text(
        textwrap.dedent("""\
        # Shared Canonical Name

        **skill_id:** sys-skill-b
        **category:** governance
        **always_apply:** true

        Content for skill B.
    """)
    )

    with patch("agentalloy.bootstrap.get_settings", return_value=_make_settings(db_path)):
        assert main([str(skill_a), "--yes"]) == EXIT_OK
        code = main([str(skill_b), "--yes"])

    assert code == EXIT_VALIDATION


def test_invalid_category_returns_validation_error(tmp_path: Path) -> None:
    db_path = str(tmp_path / "agentalloy.overgraph")
    store = open_overgraph_skill_store(db_path)  # opens + migrates
    store.close()

    bad = tmp_path / "bad_cat.md"
    bad.write_text(
        textwrap.dedent("""\
        # Some Skill

        **skill_id:** sys-some
        **category:** python
        **always_apply:** true

        Content.
    """)
    )

    with patch("agentalloy.bootstrap.get_settings", return_value=_make_settings(db_path)):
        code = main([str(bad), "--yes"])

    assert code == EXIT_VALIDATION


def test_empty_prose_is_validation_error(tmp_path: Path) -> None:
    db_path = str(tmp_path / "agentalloy.overgraph")
    store = open_overgraph_skill_store(db_path)  # opens + migrates
    store.close()

    empty = tmp_path / "empty.md"
    empty.write_text(
        textwrap.dedent("""\
        # Empty Prose

        **skill_id:** sys-empty
        **category:** governance
        **always_apply:** false
    """)
    )

    with patch("agentalloy.bootstrap.get_settings", return_value=_make_settings(db_path)):
        code = main([str(empty), "--yes"])

    assert code == EXIT_VALIDATION
