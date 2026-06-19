"""AC-1, AC-2: LadybugDB migration creates expected tables; trivial query succeeds."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentalloy.storage.ladybug import LadybugStore


@pytest.fixture
def ladybug(tmp_path: Path) -> LadybugStore:
    path = str(tmp_path / "ladybug")
    store = LadybugStore(path)
    store.open()
    store.migrate()
    return store


def test_migrate_creates_node_tables(ladybug: LadybugStore) -> None:
    rows = ladybug.execute("CALL SHOW_TABLES() RETURN *")
    names = {row[1] for row in rows}
    assert {"Skill", "SkillVersion", "Fragment"}.issubset(names)


def test_migrate_creates_rel_tables(ladybug: LadybugStore) -> None:
    rows = ladybug.execute("CALL SHOW_TABLES() RETURN *")
    names = {row[1] for row in rows}
    assert {
        "HAS_VERSION",
        "CURRENT_VERSION",
        "DECOMPOSES_TO",
        "REQUIRES_COMPOSITIONAL",
    }.issubset(names)


def test_no_vector_index_created(ladybug: LadybugStore) -> None:
    """Per v5.3, fragment embeddings live in DuckDB, not LadybugDB. The
    Kùzu VECTOR extension is not loaded and no vector index is created."""
    rows = ladybug.execute("CALL SHOW_INDEXES() RETURN *")
    # No vector index means SHOW_INDEXES returns no fragment-vector index.
    # (PRIMARY KEY indexes still exist for node tables; that's fine.)
    fragment_idx_names = {row[1] for row in rows if row and "fragment_embedding" in str(row[1])}
    assert fragment_idx_names == set()


def test_trivial_skill_count_query(ladybug: LadybugStore) -> None:
    count = ladybug.scalar("MATCH (s:Skill) RETURN count(s)")
    assert count == 0


def test_insert_and_read_back(ladybug: LadybugStore) -> None:
    ladybug.execute(
        """
        CREATE (:Skill {
            skill_id: 'test-skill',
            canonical_name: 'Test Skill',
            category: 'design',
            skill_class: 'domain',
            domain_tags: ['x'],
            deprecated: false,
            always_apply: false,
            phase_scope: [],
            category_scope: []
        })
        """
    )
    rows = ladybug.execute(
        "MATCH (s:Skill {skill_id: 'test-skill'}) RETURN s.canonical_name, s.category"
    )
    assert rows == [["Test Skill", "design"]]


# ---------------------------------------------------------------------------
# Idempotent migration (moved from test_migrate_idempotent.py)
# ---------------------------------------------------------------------------


def test_ladybug_migrate_twice(tmp_path: Path) -> None:
    """AC-5: running migrate twice against existing stores preserves data and raises nothing."""
    path = str(tmp_path / "ladybug")
    with LadybugStore(path) as store:
        store.migrate()
        store.execute(
            """
            CREATE (:Skill {
                skill_id: 's1', canonical_name: 'keep me', category: 'design',
                skill_class: 'domain', domain_tags: [], deprecated: false,
                always_apply: false, phase_scope: [], category_scope: []
            })
            """
        )

    # Reopen and migrate again — data must survive.
    with LadybugStore(path) as store:
        store.migrate()
        rows = store.execute("MATCH (s:Skill {skill_id: 's1'}) RETURN s.canonical_name")
        assert rows == [["keep me"]]
