"""AC-4: inconsistent CURRENT_VERSION state raises InconsistentActiveVersion."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentalloy.reads import InconsistentActiveVersion, get_active_skills
from agentalloy.storage.skill_store import DuckDBSkillStore, open_skill_store


@pytest.fixture
def empty_store(tmp_path: Path) -> DuckDBSkillStore:
    return open_skill_store(str(tmp_path / "agentalloy.duck"))


def _make_skill(store: DuckDBSkillStore, skill_id: str, skill_class: str = "domain") -> None:
    store.execute(
        "INSERT INTO skills (skill_id, canonical_name, category, skill_class, "
        "domain_tags, deprecated, always_apply, phase_scope, category_scope) "
        "VALUES (?, ?, 'design', ?, ?, false, false, ?, ?)",
        [skill_id, skill_id, skill_class, [], [], []],
    )


def _make_version(store: DuckDBSkillStore, skill_id: str, version_id: str, status: str) -> None:
    from datetime import UTC, datetime

    # HAS_VERSION is folded into skill_versions.skill_id.
    store.execute(
        "INSERT INTO skill_versions (version_id, skill_id, version_number, authored_at, "
        "author, change_summary, status, raw_prose) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [version_id, skill_id, 1, datetime.now(UTC), "test", "t", status, ""],
    )


def _link_current(store: DuckDBSkillStore, skill_id: str, version_id: str) -> None:
    # CURRENT_VERSION is folded into skills.current_version_id.
    store.execute(
        "UPDATE skills SET current_version_id = ? WHERE skill_id = ?",
        [version_id, skill_id],
    )


def test_current_version_points_at_superseded_raises(empty_store: DuckDBSkillStore) -> None:
    _make_skill(empty_store, "s1")
    _make_version(empty_store, "s1", "s1-v1", "superseded")
    _link_current(empty_store, "s1", "s1-v1")
    with pytest.raises(InconsistentActiveVersion) as ei:
        get_active_skills(empty_store)
    assert ei.value.skill_id == "s1"
    assert "superseded" in ei.value.reason


def test_active_version_without_current_edge_raises(empty_store: DuckDBSkillStore) -> None:
    _make_skill(empty_store, "s2")
    _make_version(empty_store, "s2", "s2-v1", "active")
    # intentionally skip _link_current
    with pytest.raises(InconsistentActiveVersion) as ei:
        get_active_skills(empty_store)
    assert ei.value.skill_id == "s2"
    assert "no CURRENT_VERSION edge" in ei.value.reason


def test_no_active_version_at_all_does_not_raise(empty_store: DuckDBSkillStore) -> None:
    # Draft-only skills are legitimately absent from active reads
    _make_skill(empty_store, "s3")
    _make_version(empty_store, "s3", "s3-v1", "draft")
    skills = get_active_skills(empty_store)
    assert skills == []


def test_empty_store_returns_empty(empty_store: DuckDBSkillStore) -> None:
    assert get_active_skills(empty_store) == []
