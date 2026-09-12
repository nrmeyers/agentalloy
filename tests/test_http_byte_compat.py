"""Byte-compat invariants of the HTTP surface (design §7, handoff contract).

TheForge consumes these shapes without code changes: ``/health`` is exact,
``/status`` is additive-only (``api_version`` 2.0, capabilities block, no
``repos`` key), ``/tool`` envelopes are ``{ok, result}`` / ``{ok, error}``,
and ``/reindex`` is a synchronous 200 that always carries ``repos`` once the
service is initialized.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agentalloy import executors, server
from agentalloy.code_index.fts import FtsDoc, FtsIndex
from agentalloy.code_index.open import fts_dir, open_codegraph
from agentalloy.code_index.protocols import CodeSymbol
from agentalloy.server import app
from agentalloy.state_store import LIFECYCLE_START, StateStore

SERVER_GLOBALS = (
    "config",
    "state_store",
    "telemetry_store",
    "skill_engine",
    "interpreter",
    "graph_store",
    "graph_searcher",
    "fts_index",
    "embed_client",
)

# Capability flags are booleans, not handles — reset to False, not None.
SERVER_FLAGS = ("graph_ready", "knowledge_ready")


def _sym(qn: str, *, file_path: str = "/repo/a.py", repo: str = "repo") -> CodeSymbol:
    return CodeSymbol(
        qualified_name=qn,
        kind="Function",
        name=qn.rsplit(".", 1)[-1],
        file_path=file_path,
        start_line=1,
        end_line=2,
        docstring=None,
        decorators=[],
        is_exported=None,
        is_async=False,
        is_generator=False,
        source_code=None,
        repo=repo,
    )


def _fake_config(tmp_path, *, service_port=48950, model_port=50001, embed_url=""):
    return type(
        "FakeConfig",
        (),
        {
            "state_duck": str(tmp_path / "state.duck"),
            "index_dir": str(tmp_path / "index"),
            "embed_url": embed_url,
            "service_port": service_port,
            "model_port": model_port,
        },
    )()


@pytest.fixture
def client():
    """TestClient with every server/executor global reset — no startup event,
    mirroring the existing server fixture so the two files can coexist."""
    saved = {n: getattr(server, n) for n in SERVER_GLOBALS + SERVER_FLAGS}
    saved_exec = (
        executors._store,
        executors._graph_store,
        executors._searcher,
        executors._skill_engine,
    )
    server._compose_state = {}
    for n in SERVER_GLOBALS:
        setattr(server, n, None)
    for n in SERVER_FLAGS:
        setattr(server, n, False)
    executors.set_store(None)
    executors.set_graph_index(None, None)
    executors.set_skill_engine(None)
    yield TestClient(app)
    for n, v in saved.items():
        setattr(server, n, v)
    (
        executors._store,
        executors._graph_store,
        executors._searcher,
        executors._skill_engine,
    ) = saved_exec


# ---------------------------------------------------------------------------
# /health and /status
# ---------------------------------------------------------------------------


def test_health_exact(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_status_uninitialized(client):
    r = client.get("/status").json()
    assert r["api_version"] == "2.0"
    assert r["capabilities"] == {
        "graph": False,
        "knowledge": False,
        "rerank": False,
        "jobs": False,
    }
    assert r["phase"] == "uninitialized"
    assert r["service_port"] == 48950
    assert r["model_port"] == 50001
    assert "repos" not in r
    assert "symbols" not in r
    assert "chunks" not in r


def test_status_initialized(client, tmp_path):
    state = StateStore(str(tmp_path / "state.duck"))
    server.state_store = state
    server.config = _fake_config(tmp_path, service_port=1234, model_port=5678)
    try:
        r = client.get("/status").json()
        assert r["phase"] == LIFECYCLE_START  # "intake"
        assert r["service_port"] == 1234
        assert r["model_port"] == 5678
        assert "repos" not in r
        assert "skills" not in r  # no skill engine wired
        assert "symbols" not in r  # no graph wired
    finally:
        state.close()


def test_status_graph_stats(client, tmp_path):
    idx = tmp_path / "index"
    st = open_codegraph(idx)
    st.upsert_symbols([_sym("a.one", file_path="/r/a.py"), _sym("b.two", file_path="/r/b.py")])
    fts = FtsIndex(fts_dir(idx))
    fts.rebuild([FtsDoc("a.one", "first symbol text")])
    state = StateStore(str(tmp_path / "state.duck"))
    # /status only exposes graph stats in the fully-initialized shape
    # (state_store + config + graph wired).
    server.state_store = state
    server.config = _fake_config(tmp_path)
    server.graph_store = st
    server.fts_index = fts
    # Capabilities key on readiness flags, not raw handles: a half-wired
    # index must not advertise features (handoff §2 — flags gate TheForge UI).
    server.graph_ready = True
    server.knowledge_ready = True
    try:
        r = client.get("/status").json()
        assert r["phase"] == LIFECYCLE_START
        assert r["symbols"] == 2
        assert r["chunks"] == 1
        assert r["capabilities"]["graph"] is True
        assert r["capabilities"]["knowledge"] is True
        assert "repos" not in r
    finally:
        state.close()
        st.close()


# ---------------------------------------------------------------------------
# /tool envelope
# ---------------------------------------------------------------------------


def test_tool_envelope_ok(client):
    r = client.post("/tool", json={"name": "telemetry", "args": "{}"}).json()
    assert r == {"ok": True, "result": '{"traces": []}'}


def test_tool_envelope_error(client):
    r = client.post("/tool", json={"name": "nope", "args": "{}"}).json()
    assert r == {"ok": False, "error": "Unknown tool: nope"}


def test_tool_result_is_parseable_json(client):
    """`result` is a JSON string the client parses — artifact_body must be a
    JSON envelope (not raw markdown), and the fallback-store record→read
    roundtrip must work."""
    import json

    rec = client.post(
        "/tool",
        json={"name": "artifact_record", "args": '{"phase": "spec", "name": "a", "body": "B"}'},
    ).json()
    assert rec["ok"] is True
    r = client.post(
        "/tool", json={"name": "artifact_body", "args": '{"phase": "spec", "name": "a"}'}
    ).json()
    assert r["ok"] is True
    assert json.loads(r["result"])["body"] == "B"


# ---------------------------------------------------------------------------
# /reindex
# ---------------------------------------------------------------------------


def test_reindex_not_initialized(client):
    r = client.post("/reindex", json={"repo": "/tmp/whatever"}).json()
    assert r == {"status": "error", "message": "service not initialized"}
    assert "repos" not in r


def test_reindex_code_index_unavailable(client, tmp_path):
    server.config = _fake_config(tmp_path)
    repo = tmp_path / "present"
    repo.mkdir()
    r = client.post("/reindex", json={"repo": str(repo)}).json()
    assert r["status"] == "error"
    assert r["message"] == "code index unavailable"
    # Registry side effect still happened: repos carries the canonical path.
    assert r["repos"] == [str(repo.expanduser().resolve())]


def test_reindex_bad_path_not_registered(client, tmp_path):
    """A nonexistent path is rejected before it can enter the registry —
    a registered bad path would be re-attempted on every restart."""
    server.config = _fake_config(tmp_path)
    r = client.post("/reindex", json={"repo": str(tmp_path / "nope")}).json()
    assert r["status"] == "error"
    assert "not a directory" in r["message"]
    assert r["repos"] == []

    from agentalloy.registry import load_repos

    assert load_repos(str(tmp_path / "state.duck")) == []


def test_reindex_allowlist_blocks_outside_roots(client, tmp_path, monkeypatch):
    server.config = _fake_config(tmp_path)
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setenv("AGENTALLOY_REPO_ALLOWLIST", str(allowed))
    r = client.post("/reindex", json={"repo": str(outside)}).json()
    assert r["status"] == "error"
    assert "allowed roots" in r["message"]
    assert r["repos"] == []


def test_reindex_graph_path_ok(client, tmp_path):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "app.py").write_text(
        'def greet(name):\n'
        '    """Say hello to the named party."""\n'
        '    return "hello " + name\n'
        "\n"
        "\n"
        "def main():\n"
        '    return greet("world")\n'
    )

    idx = tmp_path / "index"
    st = open_codegraph(idx)
    fts = FtsIndex(fts_dir(idx))
    fts.rebuild([])
    state = StateStore(str(tmp_path / "state.duck"))
    server.config = _fake_config(tmp_path)
    server.state_store = state
    server.graph_store = st
    server.fts_index = fts

    try:
        r = client.post("/reindex", json={"repo": str(repo)})
        assert r.status_code == 200  # synchronous, both outcomes
        body = r.json()
        assert body["status"] == "ok"
        assert body["symbols"] >= 2
        # Lexical-only ingest still indexes composed fallback FTS docs.
        assert body["chunks"] >= 2
        assert body["mode"] == "lexical-only"
        assert body["repos"] == [str(repo.resolve())]

        # The symbols actually landed in the shared index.
        assert st.counts_by_kind()
        assert any("greet" in s.qualified_name for s in st.symbols_matching("greet"))
    finally:
        state.close()
        st.close()
