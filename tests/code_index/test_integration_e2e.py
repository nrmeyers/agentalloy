"""Full retrieval flow against the REAL embed server (llama-server :47951).

Run manually:

    uv run pytest tests/code_index/test_integration_e2e.py -m integration -v

Indexes the committed ``fixtures/mini_repo`` (copied to tmp so the slug
derives from the directory name, not agentalloy's own git remote), then
exercises semantic search, lexical search, callers round-trip, and the
context bundle over the live index.
"""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentalloy.app import create_app
from agentalloy.code_index.api.state import CodeIndexState, get_code_index_state
from agentalloy.code_index.store import open_jobs
from agentalloy.config import Settings
from agentalloy.embed_provider import EmbedClient, get_embed_client

pytestmark = pytest.mark.integration

FIXTURE_REPO = Path(__file__).parent / "fixtures" / "mini_repo"


def _wait_for(predicate: Callable[[], bool], *, timeout_s: float = 120.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    raise AssertionError("condition not met within timeout")


def _real_embed_client(settings: Settings) -> EmbedClient:
    client = get_embed_client(settings)
    try:
        client.embed(model=settings.runtime_embedding_model, texts=["search_query: ping"])
    except Exception as exc:  # noqa: BLE001 — any failure means "server not up"
        client.close()
        pytest.skip(f"embed server unreachable at {settings.runtime_embed_base_url}: {exc}")
    return client


@pytest.fixture
def live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, Path]]:
    monkeypatch.setenv("CODE_INDEX_ENABLED", "1")
    settings = Settings(code_index_data_dir=str(tmp_path / "code-index-data"))
    embed = _real_embed_client(settings)
    # Copy the fixture out of the agentalloy checkout so repo_slug derives
    # "mini_repo" from the directory basename instead of walking up to
    # agentalloy's own git remote.
    repo = tmp_path / "mini_repo"
    shutil.copytree(FIXTURE_REPO, repo)
    state = CodeIndexState(settings=settings, embed_client=embed, jobs=open_jobs(settings))
    app = create_app(use_default_lifespan=False)
    app.dependency_overrides[get_code_index_state] = lambda: state
    with TestClient(app) as client:
        yield client, repo
    state.jobs.close()
    embed.close()


def test_index_then_retrieve(live: tuple[TestClient, Path]) -> None:
    client, repo = live

    # -- index the fixture repo via the pipeline -----------------------------
    started = client.post("/code/index", json={"repo_path": str(repo)})
    assert started.status_code == 202, started.text
    job_id = str(started.json()["id"])
    slug = str(started.json()["slug"])
    assert slug == "mini_repo"

    _wait_for(
        lambda: (
            client.get(f"/code/index/{job_id}/status").json()["state"] not in ("queued", "running")
        )
    )
    done = client.get(f"/code/index/{job_id}/status").json()
    assert done["state"] == "done", done
    assert done["symbol_count"] > 0
    assert done["embedding_count"] > 0

    # -- semantic search returns a relevant symbol ---------------------------
    semantic = client.get(
        "/code/search/semantic",
        params={"repo": slug, "q": "parse a configuration file into settings", "k": 5},
    )
    assert semantic.status_code == 200
    names = [r["qualified_name"] for r in semantic.json()]
    assert any(n.endswith("load_config") for n in names), names

    # -- lexical search finds an exact token ---------------------------------
    lexical = client.get("/code/search/lexical", params={"repo": slug, "q": "quxglobber"})
    assert lexical.status_code == 200
    lex_names = [r["qualified_name"] for r in lexical.json()]
    assert any(n.endswith("save_record") for n in lex_names), lex_names

    # -- callers round-trip ---------------------------------------------------
    validate_fqn = _fqn_of(client, slug, "validate_record")
    sym = client.get("/code/search/symbol", params={"repo": slug, "fqn": validate_fqn})
    assert sym.status_code == 200
    callers = client.get(f"/code/symbols/{validate_fqn}/callers", params={"repo": slug})
    assert callers.status_code == 200
    caller_names = [c["qualified_name"] for c in callers.json()]
    assert any(n.endswith("process_order") for n in caller_names), caller_names
    assert any(n.endswith("save_record") for n in caller_names), caller_names

    # -- context bundle assembles ---------------------------------------------
    bundle = client.post(
        "/code/context-bundle",
        json={"repo": slug, "task": "add stricter validation before records are saved"},
    )
    assert bundle.status_code == 200
    body = bundle.json()
    assert body["items"], body
    assert body["total_chars"] <= body["budget_chars"]
    assert {"seed"} <= {item["reason"] for item in body["items"]}


def _fqn_of(client: TestClient, slug: str, name: str) -> str:
    """Resolve a bare function name to its indexed fully-qualified name."""
    resp = client.get("/code/search/lexical", params={"repo": slug, "q": name, "k": 20})
    assert resp.status_code == 200
    for r in resp.json():
        if r["qualified_name"].endswith("." + name):
            return str(r["qualified_name"])
    raise AssertionError(f"{name} not found via lexical search: {resp.json()}")
