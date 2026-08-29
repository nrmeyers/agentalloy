"""Fixture loader tests.

The loader writes the skill graph only; embeddings live in the unified
corpus store's fragment index and are populated separately by the reembed
CLI. Reads below go through the SkillStore protocol.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentalloy.fixtures.loader import load_fixtures
from agentalloy.reads import get_active_fragments
from agentalloy.storage.overgraph_skill_store import OverGraphSkillStore, open_overgraph_skill_store

FIXTURE_TYPES = {"guardrail", "setup", "execution", "verification", "example", "rationale"}


@pytest.fixture
def populated_store(tmp_path: Path) -> OverGraphSkillStore:
    store = open_overgraph_skill_store(str(tmp_path / "agentalloy.overgraph"))
    load_fixtures(store)
    return store


def _all_versions(store: OverGraphSkillStore):
    out = []
    for skill in store.get_active_skills():
        out.extend(store.get_versions_by_skill(skill.skill_id))
    return out


def test_load_fixtures_counts(populated_store: OverGraphSkillStore) -> None:
    skill_count = populated_store.count_skills()
    version_count = len(_all_versions(populated_store))
    fragment_count = populated_store.count_fragments()

    # 5 domain + 3 system = 8 skills. Each has 2 versions = 16 versions.
    # Only active versions have fragments; counts summed from YAML.
    assert skill_count == 8
    assert version_count == 16
    assert fragment_count > 0


def test_every_fragment_type_present(populated_store: OverGraphSkillStore) -> None:
    present = {f.fragment_type for f in get_active_fragments(populated_store)}
    assert FIXTURE_TYPES.issubset(present), f"missing: {FIXTURE_TYPES - present}"


def test_only_active_versions_have_current_version_edge(
    populated_store: OverGraphSkillStore,
) -> None:
    # The old CURRENT_VERSION edge is folded into skills.current_version_id —
    # one per skill (all 8 have an active version).
    skills = populated_store.get_active_skills()
    assert sum(1 for s in skills if s.current_version_id) == 8


def test_superseded_versions_exist_without_current_link(
    populated_store: OverGraphSkillStore,
) -> None:
    versions = _all_versions(populated_store)
    # Each skill has one superseded version — 8 total
    superseded = [v for v in versions if v.status == "superseded"]
    assert len(superseded) == 8
    # No superseded version is pointed at by a skill's current_version_id.
    current_ids = {
        s.current_version_id for s in populated_store.get_active_skills() if s.current_version_id
    }
    assert not current_ids & {v.version_id for v in superseded}


def test_applicability_modes_covered(populated_store: OverGraphSkillStore) -> None:
    system = [s for s in populated_store.get_active_skills() if s.skill_class == "system"]

    # always_apply=true
    assert sum(1 for s in system if s.always_apply) >= 1

    scoped = [s for s in system if not s.always_apply]
    # phase_scope present
    assert sum(1 for s in scoped if s.phase_scope) >= 1
    # category_scope present
    assert sum(1 for s in scoped if s.category_scope) >= 1


def test_load_is_idempotent(tmp_path: Path) -> None:
    store = open_overgraph_skill_store(str(tmp_path / "agentalloy.overgraph"))
    first = load_fixtures(store)
    second = load_fixtures(store)
    assert first == second

    # Post second load, counts still match the first run
    assert store.count_skills() == 8


def test_fragments_loaded_without_embedding(populated_store: OverGraphSkillStore) -> None:
    """The fixture loader writes graph-only; embeddings are populated
    separately by the reembed CLI."""
    assert populated_store.count_fragments() > 0
    assert populated_store.count_embeddings() == 0


def test_active_version_fragments_are_reachable(populated_store: OverGraphSkillStore) -> None:
    # Every fragment should join back to a version (DECOMPOSES_TO folded into
    # fragments.version_id).
    for f in get_active_fragments(populated_store):
        assert populated_store.get_version(f.version_id) is not None
