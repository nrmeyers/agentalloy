"""tests/code_index/test_related_decisions.py

Unit tests for the `related_decisions` retrieval path.

Covers:
- Recall where `why` fails (MarkdownDoc with no backtick fqn)
- Result purity (only MarkdownDoc hits)
- Graceful empty (no-decisions repo → [])
- Embed budget (one embed call per query)
- RRF fusion correctness (BM25-only hit appears)
- decision_qns() returns only MarkdownDoc qualified names
- Store layer: `where` predicate on search_similar/search_bm25
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from agentalloy.code_index.api.state import CodeIndexState
from agentalloy.code_index.retrieval.hybrid import related_decisions
from agentalloy.code_index.store import open_jobs
from agentalloy.code_index.store.graph_store import DuckDBCodeGraphStore
from agentalloy.code_index.store.vector_store import LanceCodeVectorStore
from agentalloy.config import Settings
from agentalloy.storage.protocols import CodeSymbol

from .conftest import (
    FixedEmbedClient,
    axis_vec,
    make_symbol,
    seed_index,
    vector_row,
)

SLUG = "repo"


@pytest.fixture
def state(settings: Settings) -> Iterator[CodeIndexState]:
    st = CodeIndexState(
        settings=settings, embed_client=FixedEmbedClient(axis_vec(0)), jobs=open_jobs(settings)
    )
    yield st
    st.jobs.close()


def _decision_symbol(qn: str, docstring: str = "", file_path: str | None = None) -> CodeSymbol:
    """Build a MarkdownDoc symbol (decision doc)."""
    return make_symbol(
        qn,
        kind="MarkdownDoc",
        file_path=file_path,
        docstring=docstring,
        source_code=f"# Decision: {qn}\n\n{docstring}",
    )


# ---------------------------------------------------------------------------
# decision_qns() — graph store
# ---------------------------------------------------------------------------


def test_decision_qns_returns_only_markdown_docs(tmp_path: Path) -> None:
    """UT-5: decision_qns() returns only MarkdownDoc qualified names, sorted."""
    store = DuckDBCodeGraphStore(tmp_path / "graph.duck")
    store.migrate()
    store.upsert_symbols(
        [
            _decision_symbol("docs/solutions/a.md::a", "Topic A.", file_path="docs/solutions/a.md"),
            _decision_symbol("docs/solutions/b.md::b", "Topic B.", file_path="docs/solutions/b.md"),
            make_symbol("pkg.util.helper", kind="Function", file_path="pkg/util.py"),
            make_symbol("pkg.main.main", kind="Function", file_path="pkg/main.py"),
        ]
    )
    qns = store.decision_qns()
    assert qns == ["docs/solutions/a.md::a", "docs/solutions/b.md::b"]
    store.close()


def test_decision_qns_empty_repo(tmp_path: Path) -> None:
    """UT-6: decision_qns() returns [] when no MarkdownDoc symbols."""
    store = DuckDBCodeGraphStore(tmp_path / "graph.duck")
    store.migrate()
    store.upsert_symbols(
        [
            make_symbol("pkg.util.helper", kind="Function", file_path="pkg/util.py"),
        ]
    )
    assert store.decision_qns() == []
    store.close()


# ---------------------------------------------------------------------------
# Store layer — `where` predicate
# ---------------------------------------------------------------------------


def test_search_similar_where_predicate(tmp_path: Path) -> None:
    """UT-1: search_similar with `where` predicate filters correctly."""
    db_path = tmp_path / "vectors.lance"
    store = LanceCodeVectorStore(db_path)
    store.bulk_replace(
        [
            vector_row(
                "docs/solutions/a.md::a",
                axis_vec(0),
                text="topic a",
                file_path="docs/solutions/a.md",
            ),
            vector_row(
                "pkg.util.helper", axis_vec(1), text="def helper(): pass", file_path="pkg/util.py"
            ),
        ]
    )
    # With where filter — only MarkdownDoc.
    hits = store.search_similar(
        axis_vec(0),
        k=10,
        where="qualified_name IN ('docs/solutions/a.md::a', 'docs/solutions/b.md::b')",
    )
    assert len(hits) == 1
    assert hits[0].qualified_name == "docs/solutions/a.md::a"
    # Without filter — both.
    hits_all = store.search_similar(axis_vec(0), k=10)
    assert len(hits_all) == 2
    store.close()


def test_search_bm25_where_predicate(tmp_path: Path) -> None:
    """UT-2: search_bm25 with `where` predicate filters correctly."""
    db_path = tmp_path / "vectors.lance"
    store = LanceCodeVectorStore(db_path)
    store.bulk_replace(
        [
            vector_row(
                "docs/solutions/a.md::a",
                axis_vec(0),
                text="topic a content",
                file_path="docs/solutions/a.md",
            ),
            vector_row(
                "pkg.util.helper",
                axis_vec(1),
                text="def helper topic pass",
                file_path="pkg/util.py",
            ),
        ]
    )
    store.rebuild_fts_index()
    # With where filter — only MarkdownDoc.
    hits = store.search_bm25(
        "topic",
        k=10,
        where="qualified_name IN ('docs/solutions/a.md::a', 'docs/solutions/b.md::b')",
    )
    assert len(hits) == 1
    assert hits[0][0] == "docs/solutions/a.md::a"
    # Without filter — both.
    hits_all = store.search_bm25("topic", k=10)
    assert len(hits_all) == 2
    store.close()


# ---------------------------------------------------------------------------
# Retrieval layer — related_decisions()
# ---------------------------------------------------------------------------


async def test_recall_where_why_fails(state: CodeIndexState) -> None:
    """AC1: MarkdownDoc with no backtick fqn is missed by `why` but surfaced by `related_decisions`.

    A decision doc about "dependency injection" that never backticks any code symbol
    should be retrievable by thematic search via `related_decisions`.
    """
    seed_index(
        state.settings,
        SLUG,
        symbols=[
            _decision_symbol(
                "docs/solutions/di.md::di-pattern",
                docstring="Dependency injection is a pattern where dependencies are provided from outside.",
                file_path="docs/solutions/di.md",
            ),
            make_symbol(
                "pkg.app.Service",
                kind="Class",
                file_path="pkg/app.py",
                docstring="A service class.",
            ),
        ],
        vectors=[
            vector_row(
                "docs/solutions/di.md::di-pattern",
                axis_vec(0),
                text="dependency injection pattern provides dependencies from outside",
                file_path="docs/solutions/di.md",
            ),
            vector_row(
                "pkg.app.Service",
                axis_vec(1),
                text="class Service: pass",
                file_path="pkg/app.py",
            ),
        ],
    )
    results = await related_decisions(state, SLUG, "dependency injection", k=8)
    qns = [r.qualified_name for r in results]
    assert "docs/solutions/di.md::di-pattern" in qns
    assert all(r.kind == "MarkdownDoc" for r in results)


async def test_result_purity(state: CodeIndexState) -> None:
    """AC2: All results have kind == 'MarkdownDoc' even when mixed store has code symbols."""
    seed_index(
        state.settings,
        SLUG,
        symbols=[
            _decision_symbol("docs/solutions/a.md::a", "Topic A.", file_path="docs/solutions/a.md"),
            _decision_symbol("docs/solutions/b.md::b", "Topic B.", file_path="docs/solutions/b.md"),
            make_symbol(
                "pkg.util.helper", kind="Function", file_path="pkg/util.py", docstring="Helper."
            ),
            make_symbol(
                "pkg.main.main", kind="Function", file_path="pkg/main.py", docstring="Main."
            ),
        ],
        vectors=[
            vector_row(
                "docs/solutions/a.md::a",
                axis_vec(0),
                text="topic a content",
                file_path="docs/solutions/a.md",
            ),
            vector_row(
                "docs/solutions/b.md::b",
                axis_vec(0, 1),
                text="topic b content",
                file_path="docs/solutions/b.md",
            ),
            vector_row(
                "pkg.util.helper", axis_vec(0), text="def helper(): pass", file_path="pkg/util.py"
            ),
            vector_row(
                "pkg.main.main", axis_vec(1), text="def main(): pass", file_path="pkg/main.py"
            ),
        ],
    )
    results = await related_decisions(state, SLUG, "topic", k=10)
    assert all(r.kind == "MarkdownDoc" for r in results), (
        f"Non-MarkdownDoc result: {[r.kind for r in results]}"
    )
    qns = [r.qualified_name for r in results]
    assert "docs/solutions/a.md::a" in qns
    assert "docs/solutions/b.md::b" in qns


async def test_graceful_empty_no_decisions(state: CodeIndexState) -> None:
    """AC3: Repo with no decision docs returns [] with no crash."""
    seed_index(
        state.settings,
        SLUG,
        symbols=[
            make_symbol(
                "pkg.util.helper", kind="Function", file_path="pkg/util.py", docstring="Helper."
            ),
        ],
        vectors=[
            vector_row(
                "pkg.util.helper", axis_vec(0), text="def helper(): pass", file_path="pkg/util.py"
            ),
        ],
    )
    results = await related_decisions(state, SLUG, "anything", k=8)
    assert results == []


async def test_embed_budget_one_call(state: CodeIndexState) -> None:
    """AC5: Only one embed call per query."""
    client = FixedEmbedClient(axis_vec(0))
    st = CodeIndexState(
        settings=state.settings, embed_client=client, jobs=open_jobs(state.settings)
    )
    try:
        seed_index(
            st.settings,
            SLUG,
            symbols=[
                _decision_symbol(
                    "docs/solutions/x.md::x", "Topic X.", file_path="docs/solutions/x.md"
                ),
            ],
            vectors=[
                vector_row(
                    "docs/solutions/x.md::x",
                    axis_vec(0),
                    text="topic x",
                    file_path="docs/solutions/x.md",
                ),
            ],
        )
        await related_decisions(st, SLUG, "topic x", k=8)
        assert len(client.texts) == 1, f"Expected 1 embed call, got {len(client.texts)}"
    finally:
        st.jobs.close()


async def test_rrf_fusion_bm25_only_hit(
    state: CodeIndexState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RRF fusion correctness: a chunk that scores well on BM25 but poorly on dense still appears."""
    monkeypatch.setattr("agentalloy.code_index.retrieval.hybrid._FETCH_K", 2)
    seed_index(
        state.settings,
        SLUG,
        symbols=[
            _decision_symbol("docs/solutions/a.md::a", "Alpha.", file_path="docs/solutions/a.md"),
            _decision_symbol(
                "docs/solutions/b.md::b", "Zulu content here.", file_path="docs/solutions/b.md"
            ),
        ],
        vectors=[
            vector_row(
                "docs/solutions/a.md::a",
                axis_vec(0),
                text="alpha content",
                file_path="docs/solutions/a.md",
            ),
            vector_row(
                "docs/solutions/b.md::b",
                axis_vec(5),
                text="def zulu(): pass",
                file_path="docs/solutions/b.md",
            ),
        ],
        fts=True,
    )
    results = await related_decisions(state, SLUG, "zulu", k=10)
    qns = [r.qualified_name for r in results]
    assert "docs/solutions/b.md::b" in qns, "BM25-only hit should appear via RRF fusion"


async def test_hydration_fallback_stale_vector_row(state: CodeIndexState) -> None:
    """Stale vector row (no graph match) falls back gracefully — no crash."""
    seed_index(
        state.settings,
        SLUG,
        symbols=[],
        vectors=[
            vector_row(
                "ghost/phantom.md::x", axis_vec(0), text="ghost content", file_path="ghost.md"
            ),
        ],
    )
    # decision_qns() returns [] since there are no MarkdownDoc symbols.
    results = await related_decisions(state, SLUG, "ghost", k=8)
    assert results == []
