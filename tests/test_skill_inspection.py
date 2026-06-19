"""NXS-774: read-skill inspection endpoint — GET /skills/{skill_id}."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentalloy.api.skill_router import get_skill_store
from agentalloy.storage.ladybug import LadybugStore


@pytest.fixture
def populated_store(corpus_dir: Path) -> LadybugStore:
    s = LadybugStore(str(corpus_dir / "ladybug"))
    s.open()
    return s


@pytest.fixture
def client_with_store(app: FastAPI, populated_store: LadybugStore) -> TestClient:
    app.dependency_overrides[get_skill_store] = lambda: populated_store
    with TestClient(app) as c:
        return c


def _first_skill_id(store: LadybugStore) -> str:
    rows = store.execute("MATCH (s:Skill) RETURN s.skill_id LIMIT 1")
    assert rows, "fixture store has no skills"
    return str(rows[0][0])


# AC-1: known skill returns identity, class, category, active version metadata + prose
def test_known_skill_returns_full_identity(
    client_with_store: TestClient, populated_store: LadybugStore
) -> None:
    skill_id = _first_skill_id(populated_store)
    resp = client_with_store.get(f"/skills/{skill_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["skill_id"] == skill_id
    assert body["skill_class"] in ("domain", "system")
    assert body["is_active"] is True
    assert "active_version" in body
    av = body["active_version"]
    for field in (
        "version_id",
        "version_number",
        "authored_at",
        "author",
        "change_summary",
        "raw_prose",
    ):
        assert field in av, f"missing field: {field}"
    assert isinstance(av["raw_prose"], str) and av["raw_prose"]


# AC-2: active version with fragments returns fragment details
def test_active_skill_returns_fragments(
    client_with_store: TestClient, populated_store: LadybugStore
) -> None:
    skill_id = _first_skill_id(populated_store)
    resp = client_with_store.get(f"/skills/{skill_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["fragments"], list)
    if body["fragments"]:
        f = body["fragments"][0]
        for field in ("fragment_id", "fragment_type", "sequence", "content"):
            assert field in f, f"missing fragment field: {field}"


# AC-3: is_active clearly set to True for the runtime-eligible version
def test_is_active_is_true(client_with_store: TestClient, populated_store: LadybugStore) -> None:
    skill_id = _first_skill_id(populated_store)
    resp = client_with_store.get(f"/skills/{skill_id}")
    assert resp.status_code == 200
    assert resp.json()["is_active"] is True


# AC-4: missing skill returns 404, not empty/ambiguous data
def test_missing_skill_returns_404(client_with_store: TestClient) -> None:
    resp = client_with_store.get("/skills/no-such-skill-xyz")
    assert resp.status_code == 404
    body = resp.json()
    assert "detail" in body
