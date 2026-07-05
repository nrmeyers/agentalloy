"""/code/index* endpoints — 202 flow, 409 duplicate, status, cancel, delete."""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentalloy.app import create_app
from agentalloy.code_index.api.state import CodeIndexState, get_code_index_state
from agentalloy.code_index.store import code_index_paths, open_jobs
from agentalloy.config import Settings

from .conftest import FakeEmbedClient, GatedEmbedClient, write_fixture_repo


def wait_for(predicate: Callable[[], bool], *, timeout_s: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition not met within timeout")


def make_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, embed: FakeEmbedClient
) -> Iterator[tuple[TestClient, CodeIndexState]]:
    monkeypatch.setenv("CODE_INDEX_ENABLED", "1")
    settings = Settings(code_index_data_dir=str(tmp_path / "code-index-data"))
    state = CodeIndexState(settings=settings, embed_client=embed, jobs=open_jobs(settings))
    app = create_app(use_default_lifespan=False)
    app.dependency_overrides[get_code_index_state] = lambda: state
    with TestClient(app) as client:
        yield client, state
        # Drain background jobs while the portal loop is still alive; lenient
        # so an assertion inside the test body stays the primary failure.
        with contextlib.suppress(AssertionError):
            wait_for(lambda: not state.tasks, timeout_s=5.0)
    state.jobs.close()


@pytest.fixture
def fast_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, CodeIndexState]]:
    yield from make_client(tmp_path, monkeypatch, FakeEmbedClient())


@pytest.fixture
def gated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, CodeIndexState, GatedEmbedClient]]:
    embed = GatedEmbedClient()
    for client, state in make_client(tmp_path, monkeypatch, embed):
        yield client, state, embed


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "demo"
    write_fixture_repo(r)
    return r


def poll_done(client: TestClient, job_id: str, *, expect: str = "done") -> dict[str, object]:
    wait_for(
        lambda: (
            client.get(f"/code/index/{job_id}/status").json()["state"] not in ("queued", "running")
        )
    )
    body = client.get(f"/code/index/{job_id}/status").json()
    assert body["state"] == expect, body
    return body


def test_index_202_and_completion(
    fast_client: tuple[TestClient, CodeIndexState], repo: Path
) -> None:
    client, _state = fast_client
    resp = client.post("/code/index", json={"repo_path": str(repo)})
    assert resp.status_code == 202
    job = resp.json()
    assert job["slug"] == "demo"
    assert job["state"] in ("queued", "running")

    done = poll_done(client, str(job["id"]))
    assert int(str(done["symbol_count"])) > 0
    assert int(str(done["embedding_count"])) > 0
    assert done["error"] is None

    jobs = client.get("/code/index/jobs").json()
    assert [j["id"] for j in jobs] == [job["id"]]


def test_index_rejects_missing_dir(fast_client: tuple[TestClient, CodeIndexState]) -> None:
    client, _ = fast_client
    resp = client.post("/code/index", json={"repo_path": "/nonexistent/nowhere"})
    assert resp.status_code == 400


def test_duplicate_active_job_409(
    gated: tuple[TestClient, CodeIndexState, GatedEmbedClient], repo: Path
) -> None:
    client, _state, embed = gated
    first = client.post("/code/index", json={"repo_path": str(repo)})
    assert first.status_code == 202
    # The job is blocked inside the embed phase — a second request must 409.
    dup = client.post("/code/index", json={"repo_path": str(repo)})
    assert dup.status_code == 409
    assert first.json()["id"] in dup.json()["detail"]

    embed.release()
    poll_done(client, str(first.json()["id"]))


def test_job_status_404(fast_client: tuple[TestClient, CodeIndexState]) -> None:
    client, _ = fast_client
    assert client.get("/code/index/no-such-job/status").status_code == 404
    assert client.post("/code/index/no-such-job/cancel").status_code == 404


def test_cancel_active_job(
    gated: tuple[TestClient, CodeIndexState, GatedEmbedClient], repo: Path
) -> None:
    client, _state, embed = gated
    job_id = str(client.post("/code/index", json={"repo_path": str(repo)}).json()["id"])

    resp = client.post(f"/code/index/{job_id}/cancel")
    assert resp.status_code == 200

    embed.release()
    # The pipeline notices the cancel flag at the next phase boundary.
    poll_done(client, job_id, expect="cancelled")

    # Cancelling a terminal job → 409.
    assert client.post(f"/code/index/{job_id}/cancel").status_code == 409


def test_delete_repo(fast_client: tuple[TestClient, CodeIndexState], repo: Path) -> None:
    client, state = fast_client
    job_id = str(client.post("/code/index", json={"repo_path": str(repo)}).json()["id"])
    poll_done(client, job_id)

    repo_dir = code_index_paths(state.settings, "demo").repo_dir
    assert repo_dir.exists()
    resp = client.delete("/code/index/demo")
    assert resp.status_code == 200
    assert not repo_dir.exists()
    assert state.jobs.get_repo("demo") is None

    assert client.delete("/code/index/demo").status_code == 404


def test_delete_refused_while_active(
    gated: tuple[TestClient, CodeIndexState, GatedEmbedClient], repo: Path
) -> None:
    client, _state, embed = gated
    job_id = str(client.post("/code/index", json={"repo_path": str(repo)}).json()["id"])
    assert client.delete("/code/index/demo").status_code == 409
    embed.release()
    poll_done(client, job_id)
