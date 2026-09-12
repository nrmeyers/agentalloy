"""Server tests — verify REST API endpoints (T9/T10)."""

import json
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentalloy import server
from agentalloy.interpreter import Interpreter
from agentalloy.server import app
from agentalloy.state_store import StateStore
from tests.test_interpreter import MockClient, MockMessage, MockResponse, MockToolCall


def test_health_endpoint() -> None:
    """Health check returns ok."""
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_status_endpoint() -> None:
    """Status endpoint returns current phase."""
    client = TestClient(app)
    response = client.get("/status")
    assert response.status_code == 200
    data = response.json()
    assert "phase" in data
    assert "service_port" in data


def test_chat_endpoint() -> None:
    """Chat endpoint returns stub response."""
    client = TestClient(app)
    response = client.post(
        "/chat",
        json={
            "messages": [{"role": "user", "content": "test"}],
            "phase": "spec",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert "answer" in data
    assert "stop_reason" in data
    assert "steps" in data
    assert "phase" in data


@pytest.fixture
def compose_env(tmp_path: Path) -> Generator[StateStore]:
    """Real state store + mock-LLM interpreter for /compose tests.

    Resets the module-global compose activation state — it persists across
    tests, so order would otherwise matter.
    """
    store = StateStore(str(tmp_path / "state.duck"))
    client = MockClient(
        [MockResponse(MockMessage(content="Brief for the main model.")) for _ in range(8)]
    )
    server.state_store = store
    server.interpreter = Interpreter(client, state_store=store)
    server._compose_state.clear()
    yield store
    server.state_store = None
    server.interpreter = None
    server._compose_state.clear()
    store.close()


def test_compose_new_session_forces_activation(compose_env: StateStore) -> None:
    """new_session=True → activation (persona + brief) even when the phase
    is unchanged; an unflagged continuation turn gets the brief only."""
    client = TestClient(app)

    first = client.post("/compose", json={"prompt": "start work", "new_session": True})
    assert first.status_code == 200
    d1 = first.json()
    assert d1["context_type"] == 2
    assert "Phase:" in d1["context"]
    assert "Brief for the main model." in d1["context"]

    # A second session in the same phase is also oriented (the bug this
    # fixes: only the first session after a service restart got oriented).
    second = client.post("/compose", json={"prompt": "start work", "new_session": True})
    d2 = second.json()
    assert d2["context_type"] == 2
    assert "Phase:" in d2["context"]

    # Turn briefs now carry a deterministic fact block (field-report item 03:
    # briefs confabulated phase; facts come from the store, not model text).
    third = client.post("/compose", json={"prompt": "keep going"})
    d3 = third.json()
    assert d3["context_type"] == 1
    assert d3["context"].startswith("# Project State\nPhase: intake")
    assert "# Turn Brief\nBrief for the main model." in d3["context"]


def test_compose_phase_change_activates_without_flag(compose_env: StateStore) -> None:
    """A phase change activates even for a continuation turn."""
    client = TestClient(app)

    client.post("/compose", json={"prompt": "start work", "new_session": True})
    compose_env.advance_phase("design")

    result = client.post("/compose", json={"prompt": "keep going"})
    data = result.json()
    assert data["context_type"] == 2
    assert "Phase: Design" in data["context"]


def test_compose_new_session_registers_session(compose_env: StateStore) -> None:
    """A new_session compose registers an active session in the store."""
    client = TestClient(app)
    before = compose_env.list_sessions()

    client.post("/compose", json={"prompt": "start work", "new_session": True})

    after = compose_env.list_sessions()
    assert len(after) == len(before) + 1
    assert after[-1]["session_key"].startswith("sess-")
    assert after[-1]["status"] == "active"
    assert after[-1]["phase"] == "intake"


def test_compose_injects_assembled_skill(compose_env: StateStore) -> None:
    """LFM calls assemble_skill → the dynamic skill rides inside context
    (and the additive ComposeResponse fields) so the proxy needs no change."""
    from agentalloy.executors import set_skill_engine
    from agentalloy.skill_engine import SkillEngine

    client = MockClient(
        [
            MockResponse(
                MockMessage(
                    content=None,
                    tool_calls=[
                        MockToolCall(
                            "call_1",
                            "assemble_skill",
                            json.dumps({"skills": ["code-search"], "phase": "build"}),
                        )
                    ],
                )
            ),
            MockResponse(MockMessage(content="Brief.")),
        ]
    )
    server.interpreter = Interpreter(client, state_store=compose_env)
    set_skill_engine(SkillEngine())
    try:
        http = TestClient(app)
        result = http.post("/compose", json={"prompt": "find the auth code", "new_session": True})
        data = result.json()
        assert data["context_type"] == 2
        assert "## skill: code-search" in data["context"]
        assert "Provenance: 1 fragments from 1 skills" in data["context"]
        assert "Brief." in data["context"]
        assert "Phase:" in data["context"]
        # Additive structured fields
        assert "## skill: code-search" in data["skill"]
        assert data["source_skills"] == ["code-search"]
        assert data["fragments"] == ["code-search-f0"]
    finally:
        set_skill_engine(None)


def _write_fixture_repo(repo: Path) -> None:
    """A tiny parseable package for the /reindex graph tests."""
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "app.py").write_text(
        "def greet(name):\n"
        '    """Say hello to the named party."""\n'
        '    return f"Hello {name}"\n'
        "\n"
        "\n"
        "def main():\n"
        '    greet("world")\n'
    )


@pytest.fixture
def reindex_env(tmp_path: Path) -> Generator[Path, None]:
    """Minimal config + a real (empty) graph index for /reindex tests.

    No embed client: ingest runs lexical-only, so ``chunks`` is never
    asserted (FTS docs come from vector rows, and a lexical-only ingest
    writes none).
    """
    from agentalloy.code_index.fts import FtsIndex
    from agentalloy.code_index.open import fts_dir, open_codegraph

    store = open_codegraph(tmp_path / "index")
    fts = FtsIndex(fts_dir(tmp_path / "index"))
    fts.rebuild([])
    state = StateStore(str(tmp_path / "state.duck"))
    server.config = type(
        "FakeConfig",
        (),
        {
            "state_duck": str(tmp_path / "state.duck"),
            "index_dir": str(tmp_path / "index"),
            "embed_url": "",
        },
    )()
    server._compose_state.clear()
    server.graph_store = store
    server.fts_index = fts
    server.embed_client = None
    server.state_store = state
    yield tmp_path
    server.graph_store = None
    server.fts_index = None
    server.embed_client = None
    server.state_store = None
    server.config = None
    state.close()
    store.close()


def test_reindex_registers_repo_and_ingests(reindex_env: Path) -> None:
    """POST /reindex persists the repo in the registry and ingests it."""
    tmp_path = reindex_env
    repo = tmp_path / "some-repo"
    _write_fixture_repo(repo)

    client = TestClient(app)
    response = client.post("/reindex", json={"repo": str(repo)})

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["symbols"] > 0
    assert data["repos"] == [str(repo.resolve())]

    from agentalloy.registry import load_repos

    assert load_repos(str(tmp_path / "state.duck")) == [str(repo.resolve())]


def test_reindex_is_idempotent_in_registry(reindex_env: Path) -> None:
    """Re-indexing the same repo re-ingests but does not duplicate the entry."""
    tmp_path = reindex_env
    repo = tmp_path / "some-repo"
    _write_fixture_repo(repo)

    client = TestClient(app)
    for _ in range(2):
        response = client.post("/reindex", json={"repo": str(repo)})
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    data = response.json()
    assert data["repos"] == [str(repo.resolve())]

    from agentalloy.registry import load_repos

    assert load_repos(str(tmp_path / "state.duck")) == [str(repo.resolve())]


def test_reindex_without_graph_still_registers(reindex_env: Path) -> None:
    """Graph index down: the ingest fails, but the repo stays registered so a
    service restart picks it up."""
    tmp_path = reindex_env
    repo = tmp_path / "some-repo"
    _write_fixture_repo(repo)
    server.graph_store = None

    client = TestClient(app)
    response = client.post("/reindex", json={"repo": str(repo)})

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "error"
    assert data["repos"] == [str(repo.resolve())]

    from agentalloy.registry import load_repos

    assert load_repos(str(tmp_path / "state.duck")) == [str(repo.resolve())]


def test_compose_stamps_store_facts_and_scopes_project(compose_env: StateStore) -> None:
    """The fact block reflects the requesting project's scope: phase and
    contracts come from the store, and two projects don't share them."""
    client = TestClient(app)

    alpha = compose_env.scoped("alpha-11111111")
    alpha.add_contract("money-integer-cents", ["finance"], "ledger")
    alpha.advance_phase("build")

    r = client.post(
        "/compose",
        json={"prompt": "start work", "new_session": True, "project": "alpha-11111111"},
    )
    ctx = r.json()["context"]
    assert "Phase: build" in ctx
    assert "Active contracts: money-integer-cents" in ctx

    # A different project sees its own (fresh) scope.
    r2 = client.post(
        "/compose",
        json={"prompt": "start work", "new_session": True, "project": "beta-22222222"},
    )
    ctx2 = r2.json()["context"]
    assert "Phase: intake" in ctx2
    assert "money-integer-cents" not in ctx2
