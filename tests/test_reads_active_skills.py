"""AC-1, AC-3: active-version filter + skill_class filter."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentalloy.fixtures.loader import load_fixtures
from agentalloy.reads import get_active_skill_by_id, get_active_skills
from agentalloy.storage.skill_store import DuckDBSkillStore, open_skill_store


@pytest.fixture
def store(tmp_path: Path) -> DuckDBSkillStore:
    s = open_skill_store(str(tmp_path / "agentalloy.duck"))
    load_fixtures(s)
    return s


def test_returns_only_active_versions(store: DuckDBSkillStore) -> None:
    skills = get_active_skills(store)
    # 8 fixture skills (5 domain + 3 system); each has one active version
    assert len(skills) == 8
    for skill in skills:
        assert skill.active_version_id.endswith("-v2")  # fixture active = v2


def test_skill_class_filter_domain(store: DuckDBSkillStore) -> None:
    skills = get_active_skills(store, skill_class="domain")
    assert len(skills) == 5
    for s in skills:
        assert s.skill_class == "domain"


def test_skill_class_filter_system(store: DuckDBSkillStore) -> None:
    skills = get_active_skills(store, skill_class="system")
    assert len(skills) == 3
    for s in skills:
        assert s.skill_class == "system"


def test_get_by_id_returns_active(store: DuckDBSkillStore) -> None:
    s = get_active_skill_by_id(store, "py-fastapi-endpoint-design")
    assert s is not None
    assert s.active_version_id == "py-fastapi-endpoint-design-v2"
    assert s.category == "design"
    assert "python" in s.domain_tags


def test_get_by_id_unknown_returns_none(store: DuckDBSkillStore) -> None:
    assert get_active_skill_by_id(store, "does-not-exist") is None


def test_system_skills_have_applicability_fields(store: DuckDBSkillStore) -> None:
    systems = {s.skill_id: s for s in get_active_skills(store, skill_class="system")}
    assert systems["sys-governance-always"].always_apply is True
    assert systems["sys-governance-always"].phase_scope is None
    assert systems["sys-governance-build-phase"].always_apply is False
    assert systems["sys-governance-build-phase"].phase_scope == ["build"]
    assert systems["sys-governance-design-category"].category_scope == ["design"]
