"""Unit tests for the DuckDB SkillStore (v5 storage layer)."""

from __future__ import annotations

import pytest

from agentalloy.storage.skill_store import open_skill_store


@pytest.fixture
def store(tmp_path):
    ss = open_skill_store(str(tmp_path / "agentalloy.duck"))  # writer: migrates
    _seed(ss)
    yield ss
    ss.close()


def _seed(ss):
    ss.execute(
        "INSERT INTO skills (skill_id, canonical_name, skill_class, category, "
        "deprecated, current_version_id, phase_scope, domain_tags) VALUES (?,?,?,?,?,?,?,?)",
        ["sk1", "Skill One", "domain", "engineering", False, "v1", ["build"], ["python"]],
    )
    ss.execute(
        "INSERT INTO skill_versions (version_id, skill_id, version_number, status, raw_prose) "
        "VALUES (?,?,?,?,?)",
        ["v1", "sk1", 1, "active", "prose one"],
    )
    ss.execute(
        "INSERT INTO fragments (fragment_id, version_id, fragment_type, sequence, content) "
        "VALUES (?,?,?,?,?)",
        ["fr1", "v1", "execution", 0, "do the thing"],
    )
    ss.execute(
        "INSERT INTO skill_dependencies (source_skill_id, target_skill_id, rel_type) VALUES (?,?,?)",
        ["sk1", "sk2", "requires"],
    )


def test_named_params_active_skill_join(store):
    rows = store.execute(
        "SELECT s.skill_id, v.version_id FROM skills s "
        "JOIN skill_versions v ON v.version_id = s.current_version_id "
        "WHERE v.status = $st AND s.deprecated = false ORDER BY s.skill_id",
        {"st": "active"},
    )
    assert rows == [("sk1", "v1")]


def test_list_has_any_and_any_membership(store):
    assert store.execute(
        "SELECT skill_id FROM skills WHERE list_has_any(phase_scope, $p)", {"p": ["build", "qa"]}
    ) == [("sk1",)]
    assert store.execute(
        "SELECT skill_id FROM skills WHERE skill_class = ANY(?)", [["domain", "system"]]
    ) == [("sk1",)]


def test_scalar(store):
    assert store.scalar("SELECT 1") == 1


def test_corpus_meta(store):
    store.set_meta("schema_version", "1")
    store.set_meta("card_index", "cards")
    assert store.get_meta("schema_version") == "1"
    assert store.get_meta("card_index") == "cards"
    assert store.get_meta("nope") is None


def test_consistency_guard_b_clean(store):
    # active version with a CURRENT_VERSION edge -> no orphan
    assert (
        store.execute(
            "SELECT s.skill_id FROM skills s "
            "JOIN skill_versions av ON av.skill_id = s.skill_id AND av.status = 'active' "
            "LEFT JOIN skill_versions cur ON cur.version_id = s.current_version_id "
            "WHERE cur.version_id IS NULL LIMIT 1"
        )
        == []
    )


def test_delete_skill_cascade(store):
    assert store.delete_skill("sk1") == 1
    assert store.scalar("SELECT count(*) FROM skills") == 0
    assert store.scalar("SELECT count(*) FROM fragments") == 0
    assert store.scalar("SELECT count(*) FROM skill_versions") == 0
    assert store.scalar("SELECT count(*) FROM skill_dependencies") == 0


def test_rollback_batch(store):
    store.rollback_batch(["sk1", "missing"])  # soft-fail on missing
    assert store.scalar("SELECT count(*) FROM skills") == 0


def test_read_only_open(tmp_path):
    p = str(tmp_path / "agentalloy.duck")
    open_skill_store(p).close()  # create + migrate, then release
    ro = open_skill_store(p, read_only=True)
    assert ro.scalar("SELECT 1") == 1
    with pytest.raises(RuntimeError):
        ro.migrate()  # RO cannot migrate
    ro.close()
