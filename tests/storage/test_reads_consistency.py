"""AC-4: inconsistent CURRENT_VERSION state raises InconsistentActiveVersion."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentalloy.reads import InconsistentActiveVersion, get_active_skills
from agentalloy.storage.overgraph_skill_store import OverGraphSkillStore, open_overgraph_skill_store
from agentalloy.storage.protocols import SkillRow, SkillVersionRow


@pytest.fixture
def empty_store(tmp_path: Path) -> OverGraphSkillStore:
    return open_overgraph_skill_store(str(tmp_path / "agentalloy.overgraph"))


def _make_skill(store: OverGraphSkillStore, skill_id: str, skill_class: str = "domain") -> None:
    store.insert_skill(
        SkillRow(
            skill_id=skill_id,
            canonical_name=skill_id,
            category="design",
            skill_class=skill_class,
            domain_tags=[],
            deprecated=False,
            superseded_by=None,
            always_apply=False,
            phase_scope=None,
            category_scope=None,
            tier=None,
            description=None,
            current_version_id="",
        )
    )


def _make_version(store: OverGraphSkillStore, skill_id: str, version_id: str, status: str) -> None:
    store.insert_version(
        SkillVersionRow(
            version_id=version_id,
            skill_id=skill_id,
            version_number=1,
            authored_at=datetime.now(UTC),
            author="test",
            change_summary="t",
            status=status,
            raw_prose="",
        )
    )


def _link_current(store: OverGraphSkillStore, skill_id: str, version_id: str) -> None:
    skill = store.get_skill(skill_id)
    assert skill is not None
    store.insert_skill(replace(skill, current_version_id=version_id))


def test_current_version_points_at_superseded_raises(empty_store: OverGraphSkillStore) -> None:
    _make_skill(empty_store, "s1")
    _make_version(empty_store, "s1", "s1-v1", "superseded")
    _link_current(empty_store, "s1", "s1-v1")
    with pytest.raises(InconsistentActiveVersion) as ei:
        get_active_skills(empty_store)
    assert ei.value.skill_id == "s1"
    assert "superseded" in ei.value.reason


def test_active_version_without_current_edge_raises(empty_store: OverGraphSkillStore) -> None:
    _make_skill(empty_store, "s2")
    _make_version(empty_store, "s2", "s2-v1", "active")
    # insert_version creates the edge for a version that arrives already
    # active; sever it to manufacture the inconsistent state.
    empty_store._db.execute_gql("MATCH (s:Skill)-[r:CurrentVersion]->() DELETE r")
    with pytest.raises(InconsistentActiveVersion) as ei:
        get_active_skills(empty_store)
    assert ei.value.skill_id == "s2"
    assert "no CURRENT_VERSION edge" in ei.value.reason


def test_no_active_version_at_all_does_not_raise(empty_store: OverGraphSkillStore) -> None:
    # Draft-only skills are legitimately absent from active reads
    _make_skill(empty_store, "s3")
    _make_version(empty_store, "s3", "s3-v1", "draft")
    skills = get_active_skills(empty_store)
    assert skills == []


def test_empty_store_returns_empty(empty_store: OverGraphSkillStore) -> None:
    assert get_active_skills(empty_store) == []
