"""Knowledge index: repo decision docs + SDD projection into the shared graph (P3).

Covers declared front-matter edges (governs/requires/touches) and prose
extraction, idempotent re-ingest, doc-vanish purge, the suspicious-document
guard (declared governs silently disappearing), SDD contract/artifact
projection, and the end-to-end ``ingest_knowledge`` entry the /reindex
endpoint uses.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentalloy.code_index.fts import FtsIndex
from agentalloy.code_index.knowledge import (
    _resolve_target,
    ingest_knowledge,
    ingest_repo_knowledge,
    project_sdd,
)
from agentalloy.code_index.open import fts_dir, open_codegraph
from agentalloy.code_index.protocols import CodeSymbol
from agentalloy.state_store import StateStore

DOC_BODY = (
    "---\n"
    "governs:\n"
    "  - app.hello\n"
    "requires:\n"
    "  - app.world\n"
    "touches: app.helper\n"
    "---\n"
    "# Decision: hello-world\n\n"
    "We keep `app.hello` as the entry point. `app.ghost` is "
    "intentionally unresolved.\n"
)


def _sym(qn: str, *, file_path: str = "/repo/app.py", repo: str = "smoke") -> CodeSymbol:
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


def _make_repo(tmp_path):
    repo = tmp_path / "smokerepo"
    (repo / "docs" / "solutions").mkdir(parents=True)
    doc = repo / "docs" / "solutions" / "dec.md"
    doc.write_text(DOC_BODY)
    return repo, doc


@pytest.fixture
def env(tmp_path):
    store = open_codegraph(tmp_path / "index")
    repo, doc = _make_repo(tmp_path)
    app_py = str(repo / "app.py")
    store.upsert_symbols(
        [_sym(f"app.{n}", file_path=app_py) for n in ("hello", "world", "helper", "login")]
    )
    yield SimpleNamespace(
        store=store,
        repo=repo,
        doc=doc,
        index_dir=tmp_path / "index",
        state_duck=str(tmp_path / "state.duck"),
    )
    store.close()


# ---------------------------------------------------------------------------
# Repo decision docs
# ---------------------------------------------------------------------------


def test_ingest_doc_declared_and_prose_edges(env):
    report = ingest_repo_knowledge(env.store, "smoke", env.repo, embed_client=None)

    assert report.scope == "smoke"
    assert report.docs == 1
    assert report.chunks == 1
    assert report.embedded == 0
    assert report.embed_available is False

    qns = [q for q in env.store.decision_qns() if q.startswith("smoke/docs/solutions/dec.md")]
    assert len(qns) == 1
    chunk = qns[0]
    assert "smoke/docs/solutions/dec.md::" in chunk

    doc = env.store.symbol(chunk)
    assert doc is not None
    assert doc.kind == "MarkdownDoc"
    assert doc.file_path == str(env.doc)
    assert doc.repo == "smoke"

    # Declared front-matter ref + prose backtick ref to the same target are
    # two distinct provenance edges (same target/span/tier, different
    # upsert keys) — both survive, both from this chunk.
    governs = env.store.governs_edges_for_symbol("app.hello")
    assert len(governs) == 2
    assert all(e.src == chunk for e in governs)
    assert all(e.span == "app.hello" for e in governs)
    assert all(e.resolution_tier == 1 for e in governs)

    # The other declared edges (REQUIRES / TOUCHES) from the same chunk.
    out = env.store.typed_edges_from_chunks([chunk])
    assert {(e.dst, e.kind, e.resolution_tier) for e in out} == {
        ("app.world", "REQUIRES", 1),
        ("app.helper", "TOUCHES", 1),
    }

    # Unknown prose refs are dropped (no edge, no report entry) — only
    # ambiguous spans (several same-named symbols) surface in `unresolved`.
    assert report.unresolved == []
    assert env.store.typed_edges_for_fqn("app.ghost") == []
    # 4 edges: declared GOVERNS + prose GOVERNS + REQUIRES + TOUCHES.
    assert report.edges == 4


def test_ingest_idempotent(env):
    ingest_repo_knowledge(env.store, "smoke", env.repo, embed_client=None)
    before = set(env.store.decision_qns())

    report = ingest_repo_knowledge(env.store, "smoke", env.repo, embed_client=None)
    assert report.docs == 1
    assert set(env.store.decision_qns()) == before
    # No duplicate edges from the fingerprint-gated reprocess: the declared
    # + prose provenance pair is exactly what the first ingest produced.
    assert len(env.store.governs_edges_for_symbol("app.hello")) == 2
    assert env.store.count_govern_edges_for_doc(str(env.doc)) == 2


def test_doc_vanish_purges(env):
    ingest_repo_knowledge(env.store, "smoke", env.repo, embed_client=None)
    chunk = [q for q in env.store.decision_qns() if q.startswith("smoke/docs/solutions/")][0]

    env.doc.unlink()
    ingest_repo_knowledge(env.store, "smoke", env.repo, embed_client=None)

    assert not [q for q in env.store.decision_qns() if q.startswith("smoke/docs/solutions/")]
    assert env.store.count_govern_edges_for_doc(str(env.doc)) == 0
    assert env.store.symbol(chunk) is None


def test_suspicious_guard_keeps_prior_governs(env):
    ingest_repo_knowledge(env.store, "smoke", env.repo, embed_client=None)

    # The doc loses its declared governs (and the prose ref) on rewrite.
    env.doc.write_text(
        "---\n"
        "requires:\n"
        "  - app.world\n"
        "---\n"
        "# Decision: hello-world\n\n"
        "We switched the entry point.\n"
    )
    report = ingest_repo_knowledge(env.store, "smoke", env.repo, embed_client=None)

    # Flagged, and the prior GOVERNS edges are NOT deleted on suspicion.
    assert report.suspicious
    assert len(env.store.governs_edges_for_symbol("app.hello")) == 2
    out = env.store.typed_edges_from_chunks([env.store.decision_qns()[0]])
    assert any(e.dst == "app.world" and e.kind == "REQUIRES" for e in out)


# ---------------------------------------------------------------------------
# SDD projection
# ---------------------------------------------------------------------------


def test_project_sdd(env):
    state = StateStore(env.state_duck)
    try:
        state.add_contract("auth-api", ["auth"], "app.login, app.ghost")
        state.record_artifact("design", "approach", "# Approach\n\nKeep the boundary thin.\n")

        report = project_sdd(env.store, state, embed_client=None)
        assert report.scope == "sdd"
        assert report.docs == 2

        # Contracts are single-chunk: qn == uri, no anchor.
        assert "sdd://contracts/auth-api" in env.store.decision_qns()
        contract = env.store.symbol("sdd://contracts/auth-api")
        assert contract is not None
        assert contract.kind == "MarkdownDoc"
        assert contract.file_path == "sdd://contracts/auth-api"

        # Contract touches: resolved target (tier 1) + dangling (tier 0).
        edges = env.store.typed_edges_from_chunks(["sdd://contracts/auth-api"])
        assert {(e.dst, e.kind, e.resolution_tier or 0) for e in edges} == {
            ("app.login", "TOUCHES", 1),
            ("app.ghost", "TOUCHES", 0),
        }

        # Artifacts are chunked with anchors.
        artifact_qns = [
            q for q in env.store.decision_qns() if q.startswith("sdd://artifacts/design/approach::")
        ]
        assert len(artifact_qns) == 1
    finally:
        state.close()


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------


def test_ingest_knowledge_end_to_end(env):
    state = StateStore(env.state_duck)
    try:
        state.add_contract("c1", ["x"], "app.hello")
        reports = ingest_knowledge(
            env.store,
            [("smoke", env.repo)],
            state,
            embed_client=None,
            index_dir=str(env.index_dir),
        )
        assert [r.scope for r in reports] == ["smoke", "sdd"]
        assert env.store.get_meta("last_knowledge_at") is not None
        # FTS side index rebuilt. Lexical-only (no embed client) still
        # indexes composed fallback docs.
        fts = FtsIndex(fts_dir(env.index_dir))
        assert fts.exists()
        assert fts.count() > 0
    finally:
        state.close()


def test_resolve_target_tiers(env):
    # Exact FQN → tier 1.
    assert _resolve_target("app.hello", env.store) == ("app.hello", 1)

    # Unique short name → tier 2 (disambiguated against a sibling).
    env.store.upsert_symbols(
        [
            _sym("app.util.parse_int", file_path="/repo/util.py"),
            _sym("app.util.parse_float", file_path="/repo/util.py"),
        ]
    )
    assert _resolve_target("parse_int", env.store) == ("app.util.parse_int", 2)

    # Unknown → tier 0, raw dangling anchor.
    assert _resolve_target("app.unknown.thing", env.store) == ("app.unknown.thing", 0)
