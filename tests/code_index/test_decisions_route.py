"""/code/search/structural?query=governing_decisions (build 03, AC 5)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from agentalloy.app import create_app
from agentalloy.code_index.api.state import CodeIndexState, get_code_index_state
from agentalloy.code_index.store import open_jobs
from agentalloy.config import Settings
from agentalloy.storage.protocols import CodeEdge

from .conftest import FixedEmbedClient, axis_vec, make_symbol, seed_index

SLUG = "kdemo"
DECISION_QN = "docs/design/x/approach.md::why-helper"
DOC = "docs/design/x/approach.md"


def seed_decisions_repo(settings: Settings) -> None:
    seed_index(
        settings,
        SLUG,
        symbols=[
            make_symbol("pkg.util.helper", file_path="pkg/util.py"),
            make_symbol("pkg.util.orphan", file_path="pkg/util.py"),
            make_symbol(
                DECISION_QN,
                kind="MarkdownDoc",
                file_path=DOC,
                source_code="We route through `pkg.util.helper` deliberately.",
                start_line=12,
            ),
        ],
        edges=[CodeEdge(src=DECISION_QN, dst="pkg.util.helper", kind="GOVERNS", file_path=DOC)],
    )


@pytest.fixture
def client(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> Iterator[TestClient]:
    monkeypatch.setenv("CODE_INDEX_ENABLED", "1")
    seed_decisions_repo(settings)
    state = CodeIndexState(
        settings=settings, embed_client=FixedEmbedClient(axis_vec(0)), jobs=open_jobs(settings)
    )
    state.jobs.upsert_repo(
        slug=SLUG, repo_path="/repo/kdemo", data_dir=settings.code_index_data_dir
    )
    app = create_app(use_default_lifespan=False)
    app.dependency_overrides[get_code_index_state] = lambda: state
    with TestClient(app) as c:
        yield c
    state.jobs.close()


def test_governing_decisions_returns_decision_view(client: TestClient) -> None:
    resp = client.get(
        "/code/search/structural",
        params={"repo": SLUG, "query": "governing_decisions", "fqn": "pkg.util.helper"},
    )
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert len(results) == 1
    d = results[0]
    assert d["qualified_name"] == DECISION_QN
    assert d["file_path"] == DOC
    assert d["start_line"] == 12
    assert "pkg.util.helper" in d["snippet"]
    assert "heading" in d and "line" not in d  # DecisionView, not CallSiteView


def test_governing_decisions_empty_for_ungoverned(client: TestClient) -> None:
    resp = client.get(
        "/code/search/structural",
        params={"repo": SLUG, "query": "governing_decisions", "fqn": "pkg.util.orphan"},
    )
    assert resp.status_code == 200
    assert resp.json()["results"] == []


def test_governing_decisions_missing_fqn_400(client: TestClient) -> None:
    resp = client.get(
        "/code/search/structural", params={"repo": SLUG, "query": "governing_decisions"}
    )
    assert resp.status_code == 400
    assert "fqn" in resp.json()["detail"]
