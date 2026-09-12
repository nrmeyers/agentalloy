"""Executor unit tests — tool dispatch against injected engines/stores."""

import json

from agentalloy.executors import execute_tool, set_skill_engine
from agentalloy.skill_engine import Skill, SkillEngine


def test_get_skill_for_returns_engine_candidates() -> None:
    """Wired engine without pack metadata: degrades to skill rows."""
    engine = SkillEngine()  # built-in 32-skill corpus, no disk I/O, no packs
    set_skill_engine(engine)
    try:
        raw = execute_tool(
            "get_skill_for",
            json.dumps({"task": "search the codebase for auth", "phase": "build"}),
        )
        parsed = json.loads(raw)
        assert parsed["packs"] == []  # no pack metadata → no catalog
        assert parsed["count"] == len(parsed["skills"]) > 0
        assert parsed["phase"] == "build"
        for row in parsed["skills"]:
            assert row["id"]
            assert row["name"]
            assert row["description"]
            assert row["fragments"]  # breakdown dict ({"body": 1} for builtins)
    finally:
        set_skill_engine(None)


def test_get_skill_for_pack_catalog_when_packs_loaded() -> None:
    """Without packs and with pack metadata: one row per pack."""
    from agentalloy.corpus_importer import PackMeta

    engine = SkillEngine()
    engine._packs = {"test-pack": PackMeta(name="test-pack", description="TP")}
    engine.add_skill(Skill(id="tp-0", name="TP 0", pack="test-pack", description="d", body="b"))
    set_skill_engine(engine)
    try:
        parsed = json.loads(
            execute_tool("get_skill_for", json.dumps({"task": "build", "phase": "build"}))
        )
        assert parsed["count"] == 1
        assert parsed["packs"][0]["pack"] == "test-pack"
        assert parsed["packs"][0]["skills"] == 1
        assert parsed.get("skills", []) == []
    finally:
        set_skill_engine(None)


def test_get_skill_for_packs_filter() -> None:
    """With packs: skill rows only for those packs, with fragment breakdowns."""
    engine = SkillEngine()
    for i in range(3):
        engine.add_skill(
            Skill(
                id=f"tp-{i}",
                name=f"TP {i}",
                pack="test-pack",
                description="d",
                body="b",
            )
        )
    engine.add_skill(
        Skill(id="other-1", name="Other", pack="other-pack", description="d", body="b")
    )
    set_skill_engine(engine)
    try:
        parsed = json.loads(
            execute_tool(
                "get_skill_for",
                json.dumps({"task": "build", "phase": "build", "packs": ["test-pack"]}),
            )
        )
        assert parsed["packs"] == []
        assert parsed["count"] == 3
        assert [row["id"] for row in parsed["skills"]] == ["tp-0", "tp-1", "tp-2"]
        assert parsed["skills"][0]["fragments"] == {"body": 1}
    finally:
        set_skill_engine(None)


def test_get_skill_for_unwired_returns_empty() -> None:
    """No engine injected: empty result, not an error (fail-open like peers)."""
    set_skill_engine(None)
    parsed = json.loads(
        execute_tool("get_skill_for", json.dumps({"task": "anything", "phase": "build"}))
    )
    assert parsed["packs"] == []
    assert parsed["skills"] == []
    assert parsed["count"] == 0


def test_assemble_skill_wired() -> None:
    """Wired engine: deterministic assembly returns render + provenance."""
    engine = SkillEngine()
    set_skill_engine(engine)
    try:
        parsed = json.loads(
            execute_tool(
                "assemble_skill",
                json.dumps({"skills": ["code-search"], "phase": "build"}),
            )
        )
        assert "## skill: code-search" in parsed["skill"]
        assert "Provenance: 1 fragments from 1 skills" in parsed["skill"]
        assert parsed["source_skills"] == ["code-search"]
        assert parsed["dropped"] == []
    finally:
        set_skill_engine(None)


def test_assemble_skill_unwired_returns_empty() -> None:
    """No engine injected: empty skill, not an error (fail-open)."""
    set_skill_engine(None)
    parsed = json.loads(
        execute_tool(
            "assemble_skill",
            json.dumps({"skills": ["code-search"], "phase": "build"}),
        )
    )
    assert parsed["skill"] == ""
    assert parsed["fragments"] == []
    assert parsed["source_skills"] == []


class TestSkillCatalogRepoFilter:
    """Catalog rows for technologies the repo doesn't use are dropped."""

    ROWS = [
        {"id": "typescript-style", "name": "TypeScript style", "description": ""},
        {"id": "fastify-routes", "name": "Fastify routing", "description": ""},
        {"id": "python-testing", "name": "Python testing", "description": ""},
        {"id": "api-design", "name": "API design", "description": ""},
    ]

    def test_python_repo_drops_ts_and_fastify(self):
        from agentalloy.executors import _filter_rows_by_repo

        kept = _filter_rows_by_repo(list(self.ROWS), {"python", "fastapi"})
        ids = [r["id"] for r in kept]
        assert ids == ["python-testing", "api-design"]

    def test_unknown_project_no_filtering(self):
        from agentalloy.executors import _filter_rows_by_repo

        assert _filter_rows_by_repo(list(self.ROWS), None) == self.ROWS

    def test_never_filters_to_empty(self):
        from agentalloy.executors import _filter_rows_by_repo

        rows = [{"id": "typescript-style", "name": "TS", "description": ""}]
        assert _filter_rows_by_repo(rows, {"python"}) == rows

    def test_technology_free_rows_always_pass(self):
        from agentalloy.executors import _filter_rows_by_repo

        rows = [{"id": "code-review", "name": "Code review", "description": ""}]
        assert _filter_rows_by_repo(rows, {"rust"}) == rows
