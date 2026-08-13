"""Fixture loader tests.

Per v5.3 the loader writes graph-only; embeddings live in the Lance fragment
store and are populated separately by the reembed CLI. In v5 the skill graph is
DuckDB ``agentalloy.duck`` (the SkillStore), so the old Cypher reads become SQL.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentalloy.fixtures.loader import load_fixtures
from agentalloy.storage.skill_store import DuckDBSkillStore, open_skill_store

FIXTURE_TYPES = {"guardrail", "setup", "execution", "verification", "example", "rationale"}


@pytest.fixture
def populated_store(tmp_path: Path) -> DuckDBSkillStore:
    store = open_skill_store(str(tmp_path / "agentalloy.duck"))
    load_fixtures(store)
    return store


def test_load_fixtures_counts(populated_store: DuckDBSkillStore) -> None:
    skill_count = populated_store.scalar("SELECT count(*) FROM skills")
    version_count = populated_store.scalar("SELECT count(*) FROM skill_versions")
    fragment_count = populated_store.scalar("SELECT count(*) FROM fragments")

    # 5 domain + 3 system = 8 skills. Each has 2 versions = 16 versions.
    # Only active versions have fragments; counts summed from YAML.
    assert skill_count == 8
    assert version_count == 16
    assert fragment_count > 0


def test_every_fragment_type_present(populated_store: DuckDBSkillStore) -> None:
    rows = populated_store.execute("SELECT DISTINCT fragment_type FROM fragments")
    present = {row[0] for row in rows}
    assert FIXTURE_TYPES.issubset(present), f"missing: {FIXTURE_TYPES - present}"


def test_only_active_versions_have_current_version_edge(populated_store: DuckDBSkillStore) -> None:
    # The old CURRENT_VERSION edge is folded into skills.current_version_id —
    # one per skill (all 8 have an active version).
    count = populated_store.scalar(
        "SELECT count(*) FROM skills WHERE current_version_id IS NOT NULL"
    )
    assert count == 8


def test_superseded_versions_exist_without_current_link(populated_store: DuckDBSkillStore) -> None:
    # Each skill has one superseded version — 8 total
    rows = populated_store.execute(
        "SELECT version_id FROM skill_versions WHERE status = 'superseded'"
    )
    assert len(rows) == 8
    # No superseded version is pointed at by a skill's current_version_id.
    rows = populated_store.execute(
        """
        SELECT v.version_id
        FROM skill_versions v
        JOIN skills s ON s.current_version_id = v.version_id
        WHERE v.status = 'superseded'
        """
    )
    assert rows == []


def test_applicability_modes_covered(populated_store: DuckDBSkillStore) -> None:
    # always_apply=true
    always = populated_store.scalar(
        "SELECT count(*) FROM skills WHERE skill_class = 'system' AND always_apply = true"
    )
    assert always >= 1

    # phase_scope present
    phase_scoped = populated_store.scalar(
        """
        SELECT count(*) FROM skills
        WHERE skill_class = 'system' AND always_apply = false AND len(phase_scope) > 0
        """
    )
    assert phase_scoped >= 1

    # category_scope present
    category_scoped = populated_store.scalar(
        """
        SELECT count(*) FROM skills
        WHERE skill_class = 'system' AND always_apply = false AND len(category_scope) > 0
        """
    )
    assert category_scoped >= 1


def test_load_is_idempotent(tmp_path: Path) -> None:
    store = open_skill_store(str(tmp_path / "agentalloy.duck"))
    first = load_fixtures(store)
    second = load_fixtures(store)
    assert first == second

    # Post second load, counts still match the first run
    skill_count = store.scalar("SELECT count(*) FROM skills")
    assert skill_count == 8


def test_fragments_loaded_without_embedding(populated_store: DuckDBSkillStore) -> None:
    """Per v5.3, fixture loader writes graph-only; embeddings live in the Lance
    fragment store, populated separately by the reembed CLI."""
    count = populated_store.scalar("SELECT count(*) FROM fragments")
    assert count > 0


def test_active_version_fragments_are_reachable(populated_store: DuckDBSkillStore) -> None:
    # Every fragment should join back to a version (DECOMPOSES_TO folded into
    # fragments.version_id).
    fragment_count = populated_store.scalar("SELECT count(*) FROM fragments")
    reachable = populated_store.scalar(
        "SELECT count(*) FROM fragments f JOIN skill_versions v ON v.version_id = f.version_id"
    )
    assert fragment_count == reachable
