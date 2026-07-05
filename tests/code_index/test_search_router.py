"""/code/search/* endpoints — happy paths, 404s, structural 400s, bounds."""

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


def seed_demo_repo(settings: Settings) -> None:
    seed_index(
        settings,
        SLUG,
        symbols=[
            make_symbol("pkg.util.helper", file_path="pkg/util.py", docstring="Add one to x."),
            make_symbol("pkg.util.caller", file_path="pkg/util.py"),
            make_symbol("pkg.main.main", file_path="pkg/main.py"),
        ],
        edges=[
            calls_edge("pkg.util.caller", "pkg.util.helper"),
            calls_edge("pkg.main.main", "pkg.util.caller"),
        ],
        vectors=[
            vector_row(
                "pkg.util.helper",
                axis_vec(0),
                text="def helper(x): return x + 1  # zanzibar",
                file_path="pkg/util.py",
            ),
            vector_row("pkg.util.caller", axis_vec(0, 1), file_path="pkg/util.py"),
            vector_row("pkg.main.main", axis_vec(1), file_path="pkg/main.py"),
        ],
        centrality={"pkg.util.helper": 0.5, "pkg.util.caller": 0.3, "pkg.main.main": 0.2},
        fts=True,
    )


@pytest.fixture
def client(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> Iterator[TestClient]:
    monkeypatch.setenv("CODE_INDEX_ENABLED", "1")
    seed_demo_repo(settings)
    state = CodeIndexState(
        settings=settings, embed_client=FixedEmbedClient(axis_vec(0)), jobs=open_jobs(settings)
    )
    state.jobs.upsert_repo(slug=SLUG, repo_path="/repo/demo", data_dir=settings.code_index_data_dir)
    app = create_app(use_default_lifespan=False)
    app.dependency_overrides[get_code_index_state] = lambda: state
    with TestClient(app) as c:
        yield c
    state.jobs.close()


def test_semantic_search(client: TestClient) -> None:
    resp = client.get("/code/search/semantic", params={"repo": SLUG, "q": "add one", "k": 3})
    assert resp.status_code == 200
    body = resp.json()
    assert body[0]["qualified_name"] == "pkg.util.helper"
    assert body[0]["kind"] == "Function"
    assert body[0]["file_path"] == "pkg/util.py"
    assert body[0]["snippet"] == "Add one to x."
    assert len(body) == 3

    limited = client.get("/code/search/semantic", params={"repo": SLUG, "q": "add one", "k": 1})
    assert len(limited.json()) == 1


def test_lexical_search(client: TestClient) -> None:
    resp = client.get("/code/search/lexical", params={"repo": SLUG, "q": "zanzibar"})
    assert resp.status_code == 200
    assert [r["qualified_name"] for r in resp.json()] == ["pkg.util.helper"]


def test_symbol_lookup(client: TestClient) -> None:
    resp = client.get("/code/search/symbol", params={"repo": SLUG, "fqn": "pkg.util.helper"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "Function"
    assert body["docstring"] == "Add one to x."
    assert body["source_code"]

    missing = client.get("/code/search/symbol", params={"repo": SLUG, "fqn": "pkg.nope"})
    assert missing.status_code == 404


def test_files_listing(client: TestClient) -> None:
    resp = client.get("/code/search/files", params={"repo": SLUG})
    assert resp.status_code == 200
    assert resp.json() == ["pkg/main.py", "pkg/util.py"]

    prefixed = client.get("/code/search/files", params={"repo": SLUG, "prefix": "pkg/u"})
    assert prefixed.json() == ["pkg/util.py"]

    paged = client.get("/code/search/files", params={"repo": SLUG, "limit": 1, "offset": 1})
    assert paged.json() == ["pkg/util.py"]


def test_centrality_hydrated(client: TestClient) -> None:
    resp = client.get("/code/search/centrality", params={"repo": SLUG, "limit": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert [r["qualified_name"] for r in body] == ["pkg.util.helper", "pkg.util.caller"]
    assert body[0]["pagerank"] == pytest.approx(0.5)
    assert body[0]["file_path"] == "pkg/util.py"
    assert body[0]["start_line"] == 1


def test_structural_queries(client: TestClient) -> None:
    callers = client.get(
        "/code/search/structural",
        params={"repo": SLUG, "query": "callers", "fqn": "pkg.util.helper"},
    )
    assert callers.status_code == 200
    assert [s["qualified_name"] for s in callers.json()["results"]] == ["pkg.util.caller"]

    callees = client.get(
        "/code/search/structural",
        params={"repo": SLUG, "query": "callees", "fqn": "pkg.util.caller"},
    )
    assert [s["qualified_name"] for s in callees.json()["results"]] == ["pkg.util.helper"]

    transitive = client.get(
        "/code/search/structural",
        params={"repo": SLUG, "query": "transitive_callers", "fqn": "pkg.util.helper"},
    )
    assert {s["qualified_name"] for s in transitive.json()["results"]} == {
        "pkg.util.caller",
        "pkg.main.main",
    }

    counts = client.get("/code/search/structural", params={"repo": SLUG, "query": "counts_by_kind"})
    assert counts.json()["results"] == {"Function": 3}


def test_structural_unknown_query_400(client: TestClient) -> None:
    resp = client.get(
        "/code/search/structural", params={"repo": SLUG, "query": "raw_cypher", "fqn": "x"}
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    for name in ("callers", "callees", "transitive_callers", "counts_by_kind"):
        assert name in detail


def test_structural_missing_fqn_400(client: TestClient) -> None:
    resp = client.get("/code/search/structural", params={"repo": SLUG, "query": "callers"})
    assert resp.status_code == 400
    assert "fqn" in resp.json()["detail"]


def test_unknown_repo_404(client: TestClient) -> None:
    for path, params in (
        ("/code/search/semantic", {"repo": "ghost", "q": "x"}),
        ("/code/search/lexical", {"repo": "ghost", "q": "x"}),
        ("/code/search/symbol", {"repo": "ghost", "fqn": "a.b"}),
        ("/code/search/files", {"repo": "ghost"}),
        ("/code/search/centrality", {"repo": "ghost"}),
        ("/code/search/structural", {"repo": "ghost", "query": "counts_by_kind"}),
    ):
        resp = client.get(path, params=params)
        assert resp.status_code == 404, path
        assert "not indexed" in resp.json()["detail"]


def test_bounds_validation_422(client: TestClient) -> None:
    assert (
        client.get("/code/search/semantic", params={"repo": SLUG, "q": "x", "k": 0}).status_code
        == 422
    )
    assert (
        client.get("/code/search/semantic", params={"repo": SLUG, "q": "x", "k": 101}).status_code
        == 422
    )
    assert client.get("/code/search/files", params={"repo": SLUG, "limit": 0}).status_code == 422
    assert client.get("/code/search/files", params={"repo": SLUG, "limit": 101}).status_code == 422
    assert (
        client.get("/code/search/centrality", params={"repo": SLUG, "limit": 0}).status_code == 422
    )
    assert (
        client.get(
            "/code/search/structural",
            params={"repo": SLUG, "query": "transitive_callers", "fqn": "x", "depth": 0},
        ).status_code
        == 422
    )
