"""Unit tests for the bootstrap CLI.

All tests use a tmp_path LadybugStore so no live Ollama is needed.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from agentalloy.bootstrap import EXIT_OK, EXIT_USAGE, EXIT_VALIDATION, main
from agentalloy.storage.ladybug import LadybugStore

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
def seeded_db(tmp_path: Path) -> LadybugStore:
    store = LadybugStore(str(tmp_path / "ladybug"))
    store.open()
    store.migrate()
    return store


def _make_settings(db_path: str) -> object:
    class FakeSettings:
        ladybug_db_path = db_path

    return FakeSettings()


def test_insert_new_skill(tmp_path: Path, md_file: Path) -> None:
    db_path = str(tmp_path / "ladybug")
    store = LadybugStore(db_path)
    store.open()
    store.migrate()
    store.close()

    with patch("agentalloy.bootstrap.get_settings", return_value=_make_settings(db_path)):
        code = main([str(md_file), "--yes"])

    assert code == EXIT_OK

    store.open()
    name = store.scalar("MATCH (s:Skill {skill_id: 'sys-sample'}) RETURN s.canonical_name")
    assert name == "Sample Governance Rule"

    version_count = store.scalar(
        "MATCH (:Skill {skill_id: 'sys-sample'})-[:HAS_VERSION]->(v) RETURN count(v)"
    )
    assert version_count == 1

    fragment_count = store.scalar(
        """
        MATCH (:Skill {skill_id: 'sys-sample'})-[:HAS_VERSION]->(v)-[:DECOMPOSES_TO]->(f)
        RETURN count(f)
        """
    )
    assert fragment_count == 1

    current = store.scalar(
        "MATCH (:Skill {skill_id: 'sys-sample'})-[:CURRENT_VERSION]->(v) RETURN v.version_id"
    )
    assert current == "sys-sample-v1"
    store.close()


def test_init_schema_flag(tmp_path: Path, md_file: Path) -> None:
    db_path = str(tmp_path / "ladybug_new")
    # DB doesn't exist yet — --init-schema must create schema first
    with patch("agentalloy.bootstrap.get_settings", return_value=_make_settings(db_path)):
        code = main([str(md_file), "--init-schema", "--yes"])

    assert code == EXIT_OK

    store = LadybugStore(db_path)
    store.open()
    count = store.scalar("MATCH (s:Skill) RETURN count(s)")
    assert count == 1
    store.close()


def test_duplicate_without_force_fails(tmp_path: Path, md_file: Path) -> None:
    db_path = str(tmp_path / "ladybug")
    store = LadybugStore(db_path)
    store.open()
    store.migrate()
    store.close()

    with patch("agentalloy.bootstrap.get_settings", return_value=_make_settings(db_path)):
        main([str(md_file), "--yes"])
        code = main([str(md_file), "--yes"])

    assert code == EXIT_VALIDATION


def test_force_overwrites(tmp_path: Path, md_file: Path) -> None:
    db_path = str(tmp_path / "ladybug")
    store = LadybugStore(db_path)
    store.open()
    store.migrate()
    store.close()

    with patch("agentalloy.bootstrap.get_settings", return_value=_make_settings(db_path)):
        main([str(md_file), "--yes"])
        code = main([str(md_file), "--force", "--yes"])

    assert code == EXIT_OK

    store.open()
    count = store.scalar("MATCH (s:Skill {skill_id: 'sys-sample'}) RETURN count(s)")
    assert count == 1
    store.close()


def test_file_not_found_returns_usage_error() -> None:
    code = main(["/nonexistent/skill.md", "--yes"])
    assert code == EXIT_USAGE


def test_invalid_markdown_returns_validation_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.md"
    bad.write_text("no heading here\n\n**skill_id:** sys-x\n")
    code = main([str(bad), "--yes"])
    assert code == EXIT_VALIDATION


def test_non_sys_prefix_returns_validation_error(tmp_path: Path) -> None:
    db_path = str(tmp_path / "ladybug")
    store = LadybugStore(db_path)
    store.open()
    store.migrate()
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
    db_path = str(tmp_path / "ladybug")
    store = LadybugStore(db_path)
    store.open()
    store.migrate()
    store.close()

    md = tmp_path / "phase.md"
    md.write_text(_PHASE_MD)

    with patch("agentalloy.bootstrap.get_settings", return_value=_make_settings(db_path)):
        code = main([str(md), "--yes"])

    assert code == EXIT_OK

    store.open()
    row = store.execute(
        "MATCH (s:Skill {skill_id: 'sys-build-rule'}) RETURN s.always_apply, s.phase_scope"
    )
    assert row[0][0] is False
    assert "build" in row[0][1]
    store.close()


def test_sdd_fast_phase_scope_is_valid(tmp_path: Path) -> None:
    """sys skills can scope to the fast-lane phase — `sdd-fast` is in the
    canonical lifecycle vocabulary, not rejected as unknown."""
    db_path = str(tmp_path / "ladybug")
    store = LadybugStore(db_path)
    store.open()
    store.migrate()
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
    store.open()
    row = store.execute("MATCH (s:Skill {skill_id: 'sys-fast-rule'}) RETURN s.phase_scope")
    assert "sdd-fast" in row[0][0]
    store.close()


def test_always_apply_with_phase_scope_is_validation_error(tmp_path: Path) -> None:
    db_path = str(tmp_path / "ladybug")
    store = LadybugStore(db_path)
    store.open()
    store.migrate()
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
    db_path = str(tmp_path / "ladybug")
    store = LadybugStore(db_path)
    store.open()
    store.migrate()
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
    db_path = str(tmp_path / "ladybug")
    store = LadybugStore(db_path)
    store.open()
    store.migrate()
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
    db_path = str(tmp_path / "ladybug")
    store = LadybugStore(db_path)
    store.open()
    store.migrate()
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
