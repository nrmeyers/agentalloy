"""POST /code/context-bundle — happy path, unknown repo, validation."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from agentalloy.app import create_app
from agentalloy.code_index.api.state import CodeIndexState, get_code_index_state
from agentalloy.code_index.store import open_jobs
from agentalloy.config import Settings

from .conftest import FixedEmbedClient, axis_vec, calls_edge, make_symbol, seed_index, vector_row

SLUG = "demo"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[TestClient]:
    monkeypatch.setenv("CODE_INDEX_ENABLED", "1")
    seed_index(
        settings,
        SLUG,
        symbols=[
            make_symbol("pkg.core", source_code="def core():\n    return leaf()"),
            make_symbol("pkg.entry", source_code="def entry():\n    return core()"),
            make_symbol("pkg.leaf", source_code="def leaf():\n    return 42"),
        ],
        edges=[calls_edge("pkg.entry", "pkg.core"), calls_edge("pkg.core", "pkg.leaf")],
        vectors=[vector_row("pkg.core", axis_vec(0))],
    )
    state = CodeIndexState(
        settings=settings, embed_client=FixedEmbedClient(axis_vec(0)), jobs=open_jobs(settings)
    )
    state.jobs.upsert_repo(slug=SLUG, repo_path="/repo/demo", data_dir=settings.code_index_data_dir)
    app = create_app(use_default_lifespan=False)
    app.dependency_overrides[get_code_index_state] = lambda: state
    with TestClient(app) as c:
        yield c
    state.jobs.close()


def test_context_bundle_happy_path(client: TestClient) -> None:
    resp = client.post(
        "/code/context-bundle", json={"repo": SLUG, "task": "explain the core routine"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["repo"] == SLUG
    assert body["budget_chars"] == 24000
    assert body["total_chars"] <= body["budget_chars"]
    reasons = {item["qualified_name"]: item["reason"] for item in body["items"]}
    assert reasons == {"pkg.core": "seed", "pkg.entry": "caller", "pkg.leaf": "callee"}
    assert all(item["file_path"] for item in body["items"])


def test_context_bundle_custom_budget(client: TestClient) -> None:
    resp = client.post(
        "/code/context-bundle",
        json={"repo": SLUG, "task": "explain the core routine", "budget_chars": 500},
    )
    assert resp.status_code == 200
    assert resp.json()["total_chars"] <= 500


def test_context_bundle_unknown_repo_404(client: TestClient) -> None:
    resp = client.post("/code/context-bundle", json={"repo": "ghost", "task": "anything"})
    assert resp.status_code == 404
    assert "not indexed" in resp.json()["detail"]


def test_context_bundle_validation_422(client: TestClient) -> None:
    # Budget below the floor.
    assert (
        client.post(
            "/code/context-bundle",
            json={"repo": SLUG, "task": "x", "budget_chars": 10},
        ).status_code
        == 422
    )
    # Missing / empty task.
    assert client.post("/code/context-bundle", json={"repo": SLUG}).status_code == 422
    assert client.post("/code/context-bundle", json={"repo": SLUG, "task": ""}).status_code == 422
