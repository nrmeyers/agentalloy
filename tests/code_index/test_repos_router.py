"""/code/repos* endpoints — listing, stats, reindex."""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentalloy.app import create_app
from agentalloy.code_index.api.state import CodeIndexState, get_code_index_state
from agentalloy.code_index.store import open_jobs
from agentalloy.config import Settings

from .conftest import FakeEmbedClient, write_fixture_repo


def wait_for(predicate: Callable[[], bool], *, timeout_s: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition not met within timeout")


@pytest.fixture
def env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, CodeIndexState, FakeEmbedClient]]:
    monkeypatch.setenv("CODE_INDEX_ENABLED", "1")
    settings = Settings(code_index_data_dir=str(tmp_path / "code-index-data"))
    embed = FakeEmbedClient()
    state = CodeIndexState(settings=settings, embed_client=embed, jobs=open_jobs(settings))
    app = create_app(use_default_lifespan=False)
    app.dependency_overrides[get_code_index_state] = lambda: state
    with TestClient(app) as client:
        yield client, state, embed
        with contextlib.suppress(AssertionError):
            wait_for(lambda: not state.tasks, timeout_s=5.0)
    state.jobs.close()


def index_and_wait(client: TestClient, repo: Path) -> str:
    resp = client.post("/code/index", json={"repo_path": str(repo)})
    assert resp.status_code == 202
    job_id = str(resp.json()["id"])
    wait_for(
        lambda: (
            client.get(f"/code/index/{job_id}/status").json()["state"] not in ("queued", "running")
        )
    )
    assert client.get(f"/code/index/{job_id}/status").json()["state"] == "done"
    return job_id


def test_repos_empty(env: tuple[TestClient, CodeIndexState, FakeEmbedClient]) -> None:
    client, _, _ = env
    assert client.get("/code/repos").json() == []


def test_repos_listing_and_stats(
    env: tuple[TestClient, CodeIndexState, FakeEmbedClient], tmp_path: Path
) -> None:
    client, _, _ = env
    repo = tmp_path / "demo"
    write_fixture_repo(repo)
    index_and_wait(client, repo)

    repos = client.get("/code/repos").json()
    assert len(repos) == 1
    view = repos[0]
    assert view["slug"] == "demo"
    assert view["repo_path"] == str(repo)
    assert view["last_indexed_at"] is not None
    assert view["symbol_count"] > 0
    assert view["edge_count"] > 0

    stats = client.get("/code/repos/demo/stats")
    assert stats.status_code == 200
    body = stats.json()
    assert body["slug"] == "demo"
    assert body["counts_by_kind"]["Function"] == 3
    assert body["vector_count"] > 0
    top = body["top_centrality"]
    assert top and {"qualified_name", "pagerank"} <= set(top[0])


def test_stats_unknown_repo_404(env: tuple[TestClient, CodeIndexState, FakeEmbedClient]) -> None:
    client, _, _ = env
    assert client.get("/code/repos/ghost/stats").status_code == 404


def test_reindex(env: tuple[TestClient, CodeIndexState, FakeEmbedClient], tmp_path: Path) -> None:
    client, _, embed = env
    repo = tmp_path / "demo"
    write_fixture_repo(repo)
    index_and_wait(client, repo)
    embeds_before = len(embed.embedded_texts)

    resp = client.post("/code/repos/demo/reindex")
    assert resp.status_code == 202
    job_id = str(resp.json()["id"])
    wait_for(
        lambda: (
            client.get(f"/code/index/{job_id}/status").json()["state"] not in ("queued", "running")
        )
    )
    assert client.get(f"/code/index/{job_id}/status").json()["state"] == "done"
    # Reindex is force=True: everything re-embeds despite unchanged content.
    assert len(embed.embedded_texts) > embeds_before


def test_reindex_unknown_repo_404(
    env: tuple[TestClient, CodeIndexState, FakeEmbedClient],
) -> None:
    client, _, _ = env
    assert client.post("/code/repos/ghost/reindex").status_code == 404
