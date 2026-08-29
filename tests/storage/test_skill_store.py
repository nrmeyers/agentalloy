"""Unit tests for the unified OverGraph corpus store (skill side)."""

from __future__ import annotations

import datetime as _dt

import pytest

from agentalloy.storage.overgraph_skill_store import open_overgraph_skill_store
from agentalloy.storage.protocols import (
    FragmentRow,
    SkillDependencyRow,
    SkillRow,
    SkillVersionRow,
)

_UTC = _dt.UTC


@pytest.fixture
def store(tmp_path):
    ss = open_overgraph_skill_store(str(tmp_path / "agentalloy.overgraph"))  # writer: migrates
    _seed(ss)
    yield ss
    ss.close()


def _seed(ss):
    now = _dt.datetime.now(tz=_UTC)
    ss.insert_skill(
        SkillRow(
            skill_id="sk1",
            canonical_name="Skill One",
            category="engineering",
            skill_class="domain",
            domain_tags=["python"],
            deprecated=False,
            superseded_by=None,
            always_apply=False,
            phase_scope=["build"],
            category_scope=None,
            tier=None,
            description=None,
            current_version_id="v1",
        )
    )
    ss.insert_version(
        SkillVersionRow(
            version_id="v1",
            skill_id="sk1",
            version_number=1,
            authored_at=now,
            author="",
            change_summary="",
            status="active",
            raw_prose="prose one",
        )
    )
    ss.insert_fragment(
        FragmentRow(
            fragment_id="fr1",
            version_id="v1",
            fragment_type="execution",
            sequence=0,
            content="do the thing",
        )
    )
    ss.insert_dependency(
        SkillDependencyRow(source_skill_id="sk1", target_skill_id="sk2", rel_type="requires")
    )


def test_corpus_meta(store):
    store.set_meta("schema_version", "1")
    store.set_meta("card_index", "cards")
    assert store.get_meta("schema_version") == "1"
    assert store.get_meta("card_index") == "cards"
    assert store.get_meta("nope") is None


def test_delete_skill_cascade(store):
    assert store.count_skills() == 1
    assert store.count_fragments() == 1
    assert store.delete_skill("sk1") == 1
    assert store.count_skills() == 0
    assert store.count_fragments() == 0
    assert store.get_version("v1") is None
    assert store.get_dependencies("sk1") == []


def test_rollback_batch(store):
    store.rollback_batch(["sk1", "missing"])  # soft-fail on missing
    assert store.count_skills() == 0


def test_read_only_open(tmp_path):
    p = str(tmp_path / "agentalloy.overgraph")
    open_overgraph_skill_store(p).close()  # create + migrate, then release
    ro = open_overgraph_skill_store(p, read_only=True)
    assert ro.count_skills() == 0
    with pytest.raises(RuntimeError):
        ro.migrate()  # RO cannot migrate
    ro.close()


def test_released_reconnects_and_sees_writer_changes(tmp_path):
    """The service holds the store read-only for its lifetime; released()
    closes the handle so an in-process writer (web reembed / pack install)
    can attach, then reconnects — the object stays valid for its holders."""
    p = str(tmp_path / "agentalloy.overgraph")
    open_overgraph_skill_store(p).close()  # create + migrate, then release

    holder = open_overgraph_skill_store(p, read_only=True)
    with holder.released():
        w = open_overgraph_skill_store(p)
        w.set_meta("k", "v")
        w.close()
    assert holder.get_meta("k") == "v"
    holder.close()
