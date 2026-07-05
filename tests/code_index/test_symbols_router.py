"""/code/symbols/* endpoints — dotted-fqn paths, callers depth, 404s."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from agentalloy.app import create_app
from agentalloy.code_index.api.state import CodeIndexState, get_code_index_state
from agentalloy.code_index.store import open_jobs
from agentalloy.config import Settings

from .conftest import FixedEmbedClient, axis_vec, calls_edge, make_symbol, seed_index

SLUG = "demo"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[TestClient]:
    monkeypatch.setenv("CODE_INDEX_ENABLED", "1")
    seed_index(
        settings,
        SLUG,
        symbols=[
            make_symbol("pkg.util.helper", file_path="pkg/util.py", docstring="Add one."),
            make_symbol("pkg.util.caller", file_path="pkg/util.py"),
            make_symbol("pkg.main.main", file_path="pkg/main.py"),
        ],
        edges=[
            calls_edge("pkg.util.caller", "pkg.util.helper"),
            calls_edge("pkg.main.main", "pkg.util.caller"),
        ],
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


def test_symbol_detail_dotted_fqn(client: TestClient) -> None:
    resp = client.get("/code/symbols/pkg.util.helper", params={"repo": SLUG})
    assert resp.status_code == 200
    body = resp.json()
    assert body["qualified_name"] == "pkg.util.helper"
    assert body["docstring"] == "Add one."
    assert body["file_path"] == "pkg/util.py"


def test_symbol_detail_404(client: TestClient) -> None:
    resp = client.get("/code/symbols/pkg.no.such", params={"repo": SLUG})
    assert resp.status_code == 404
    assert "pkg.no.such" in resp.json()["detail"]


def test_callers_direct_and_transitive(client: TestClient) -> None:
    direct = client.get("/code/symbols/pkg.util.helper/callers", params={"repo": SLUG})
    assert direct.status_code == 200
    assert [s["qualified_name"] for s in direct.json()] == ["pkg.util.caller"]

    deep = client.get("/code/symbols/pkg.util.helper/callers", params={"repo": SLUG, "depth": 3})
    assert {s["qualified_name"] for s in deep.json()} == {"pkg.util.caller", "pkg.main.main"}


def test_callees(client: TestClient) -> None:
    resp = client.get("/code/symbols/pkg.util.caller/callees", params={"repo": SLUG})
    assert resp.status_code == 200
    sites = resp.json()
    assert [s["qualified_name"] for s in sites] == ["pkg.util.helper"]
    assert sites[0]["file_path"] == "pkg/util.py"


def test_unknown_repo_404(client: TestClient) -> None:
    for path in (
        "/code/symbols/pkg.util.helper",
        "/code/symbols/pkg.util.helper/callers",
        "/code/symbols/pkg.util.caller/callees",
    ):
        resp = client.get(path, params={"repo": "ghost"})
        assert resp.status_code == 404, path
        assert "not indexed" in resp.json()["detail"]


def test_depth_bounds_422(client: TestClient) -> None:
    resp = client.get("/code/symbols/pkg.util.helper/callers", params={"repo": SLUG, "depth": 0})
    assert resp.status_code == 422
    resp = client.get("/code/symbols/pkg.util.helper/callers", params={"repo": SLUG, "depth": 11})
    assert resp.status_code == 422
