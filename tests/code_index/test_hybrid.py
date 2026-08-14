"""retrieval.hybrid — dense ranking, pagerank fusion, RRF/BM25 merge, rewrite."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from agentalloy.code_index.api.state import CodeIndexState
from agentalloy.code_index.retrieval import hybrid
from agentalloy.code_index.retrieval.hybrid import (
    finalize_query_text,
    lexical_search,
    related_decisions,
    rewrite_query,
    semantic_search,
)
from agentalloy.code_index.store import open_jobs
from agentalloy.config import Settings
from agentalloy.storage.protocols import CodeEdge, CodeSymbol

from .conftest import (
    FixedEmbedClient,
    axis_vec,
    make_symbol,
    mix_vec,
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


async def test_dense_ranking_respected(state: CodeIndexState) -> None:
    """No pagerank, no FTS index: results follow cosine order."""
    seed_index(
        state.settings,
        SLUG,
        symbols=[
            make_symbol("m.exact", docstring="The exact one."),
            make_symbol("m.partial"),
            make_symbol("m.orthogonal"),
        ],
        vectors=[
            vector_row("m.exact", axis_vec(0)),  # cosine 1.0
            vector_row("m.partial", axis_vec(0, 1)),  # cosine ~0.707
            vector_row("m.orthogonal", axis_vec(1)),  # cosine 0.0
        ],
    )
    results = await semantic_search(state, SLUG, "anything", k=10)
    assert [r.qualified_name for r in results] == ["m.exact", "m.partial", "m.orthogonal"]
    assert results[0].kind == "Function"
    assert results[0].file_path == "m/exact.py"
    assert results[0].snippet == "The exact one."
    assert results[0].score > results[1].score > results[2].score

    # k bound.
    assert len(await semantic_search(state, SLUG, "anything", k=2)) == 2


async def test_pagerank_fusion_reorders(state: CodeIndexState) -> None:
    """A low-cosine / high-pagerank symbol overtakes higher-cosine ones."""
    symbols = [make_symbol(qn) for qn in ("m.a", "m.b", "m.c")]
    vectors = [
        vector_row("m.a", axis_vec(0)),  # cosine 1.0
        vector_row("m.b", mix_vec(0, 1, 0.8, 0.6)),  # cosine 0.8
        vector_row("m.c", mix_vec(0, 1, 0.6, 0.8)),  # cosine 0.6
    ]
    # Without centrality the order is a, b, c.
    seed_index(state.settings, SLUG, symbols=symbols, vectors=vectors)
    baseline = await semantic_search(state, SLUG, "anything", k=3)
    assert [r.qualified_name for r in baseline] == ["m.a", "m.b", "m.c"]

    # 0.7*0.6 + 0.3*1.0 = 0.72 for m.c vs 0.7 for m.a vs 0.56 for m.b.
    seed_index(
        state.settings, "boosted", symbols=symbols, vectors=vectors,
        centrality={"m.a": 0.0, "m.b": 0.0, "m.c": 0.9},
    )  # fmt: skip
    boosted = await semantic_search(state, "boosted", "anything", k=3)
    assert [r.qualified_name for r in boosted] == ["m.c", "m.a", "m.b"]


async def test_rrf_merges_bm25_only_hit(
    state: CodeIndexState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hit outside the dense candidate pool enters via the BM25 leg."""
    monkeypatch.setattr(hybrid, "_FETCH_K", 2)
    seed_index(
        state.settings,
        SLUG,
        symbols=[make_symbol(qn) for qn in ("m.a", "m.b", "m.lexical")],
        vectors=[
            vector_row("m.a", axis_vec(0)),
            vector_row("m.b", axis_vec(0, 1)),
            # Orthogonal to the query — never in the dense top-2 — but the
            # only BM25 match for "zanzibar".
            vector_row("m.lexical", axis_vec(5), text="def lexical(): return 'zanzibar'"),
        ],
        fts=True,
    )
    results = await semantic_search(state, SLUG, "zanzibar", k=10)
    names = [r.qualified_name for r in results]
    assert "m.lexical" in names
    # RRF: dense [a, b] + bm25 [lexical] → a and lexical tie at 1/61, b at 1/62.
    assert names == ["m.a", "m.lexical", "m.b"]


async def test_lexical_search_hydrates_from_graph(state: CodeIndexState) -> None:
    seed_index(
        state.settings,
        SLUG,
        symbols=[make_symbol("m.frob", docstring="Frobnicates widgets.")],
        vectors=[vector_row("m.frob", axis_vec(0), text="def frobnicate(widget): pass")],
        fts=True,
    )
    results = await lexical_search(state, SLUG, "frobnicate", k=5)
    assert [r.qualified_name for r in results] == ["m.frob"]
    assert results[0].snippet == "Frobnicates widgets."
    assert results[0].score > 0.0

    # No FTS match — empty, not an error.
    assert await lexical_search(state, SLUG, "nomatchtoken", k=5) == []


def test_rewrite_query_essence() -> None:
    # Long descriptive query: stop-words stripped.
    assert rewrite_query("how does the error envelope construction work") == (
        "error envelope construction work"
    )
    # Short queries pass through.
    assert rewrite_query("error envelope") == "error envelope"
    # Symbol-like tokens pass through.
    assert rewrite_query("where is pkg.util.helper defined exactly") == (
        "where is pkg.util.helper defined exactly"
    )
    # Over-strip guard: all-filler queries pass through.
    assert rewrite_query("how does it do that") == "how does it do that"


def test_finalize_query_text_prefix_and_cap() -> None:
    out = finalize_query_text("x" * 10_000)
    assert out.startswith("search_query: ")
    assert len(out) <= hybrid.MAX_EMBED_TEXT_CHARS


# ---------------------------------------------------------------------------
# related_decisions — entity expansion
# ---------------------------------------------------------------------------


def _decision_symbol(qn: str, heading: str, body: str) -> CodeSymbol:
    """A MarkdownDoc symbol that acts as a decision chunk."""
    from .conftest import make_symbol
    return make_symbol(
        qn,
        kind="MarkdownDoc",
        file_path=qn.split("::")[0] if "::" in qn else f"{qn}.md",
        docstring=body,
        source_code=f"# {heading}\n\n{body}",
    )


async def test_related_decisions_no_entity_edges(state: CodeIndexState) -> None:
    """No regression: semantic-only results when no entity edges exist."""
    state.jobs.upsert_repo(slug=SLUG, repo_path="/repo/test", data_dir=state.settings.code_index_data_dir)
    seed_index(
        state.settings,
        SLUG,
        symbols=[
            _decision_symbol("docs/auth.md::rate-limiting", "Rate Limiting", "Limit requests per user."),
            _decision_symbol("docs/auth.md::auth-middleware", "Auth Middleware", "Middleware for auth."),
        ],
        vectors=[
            vector_row("docs/auth.md::rate-limiting", axis_vec(0), text="Limit requests per user."),
            vector_row("docs/auth.md::auth-middleware", axis_vec(0, 1), text="Middleware for auth."),
        ],
        fts=True,
        repo_path="/repo/test",
    )

    results = await related_decisions(state, SLUG, "rate limiting", k=5, repo_path="/repo/test")
    assert len(results) >= 1
    assert all(r.connected_via is None for r in results)


async def test_related_decisions_entity_expansion(state: CodeIndexState) -> None:
    """Entity edges from semantic results surface additional decisions."""
    state.jobs.upsert_repo(slug=SLUG, repo_path="/repo/test", data_dir=state.settings.code_index_data_dir)
    seed_index(
        state.settings,
        SLUG,
        symbols=[
            _decision_symbol("docs/auth.md::auth-middleware", "Auth Middleware", "Auth middleware design."),
            _decision_symbol("docs/rate.md::rate-limiting", "Rate Limiting", "Rate limiting policy."),
            make_symbol("src.auth.middleware", docstring="Auth middleware implementation."),
        ],
        edges=[
            # Entity edge from auth decision → rate limiter symbol
            CodeEdge(
                src="docs/auth.md::auth-middleware",
                dst="src.auth.middleware",
                kind="CONSTRAINTS",
                file_path="docs/auth.md",
            ),
            # GOVERNS edge: rate-limiting decision governs the rate limiter
            CodeEdge(
                src="docs/rate.md::rate-limiting",
                dst="src.auth.middleware",
                kind="GOVERNS",
                file_path="docs/rate.md",
            ),
        ],
        vectors=[
            vector_row("docs/auth.md::auth-middleware", axis_vec(0), text="Auth middleware design."),
            vector_row("docs/rate.md::rate-limiting", axis_vec(1), text="Rate limiting policy."),
            vector_row("src.auth.middleware", axis_vec(0), text="Auth middleware implementation."),
        ],
        fts=True,
        repo_path="/repo/test",
    )

    results = await related_decisions(state, SLUG, "auth middleware", k=5, repo_path="/repo/test")
    # Semantic match first
    semantic_qns = [r.qualified_name for r in results if r.connected_via is None]
    assert "docs/auth.md::auth-middleware" in semantic_qns

    # Entity-connected result: rate-limiting decision connected via CONSTRAINTS
    entity_results = [r for r in results if r.connected_via is not None]
    assert len(entity_results) >= 1
    assert entity_results[0].connected_via == "CONSTRAINTS"
    assert entity_results[0].qualified_name == "docs/rate.md::rate-limiting"


async def test_related_decisions_entity_cap(state: CodeIndexState) -> None:
    """Entity expansion capped at 3."""
    state.jobs.upsert_repo(slug=SLUG, repo_path="/repo/test", data_dir=state.settings.code_index_data_dir)
    symbols = [
        _decision_symbol("docs/main.md::main-decision", "Main", "Main decision about everything."),
    ]
    edges = []
    for i in range(5):
        dst = f"src.mod{i}.fn"
        symbols.append(make_symbol(dst, docstring=f"Module {i} function."))
        edges.append(CodeEdge(
            src="docs/main.md::main-decision",
            dst=dst,
            kind="TOUCHES",
            file_path="docs/main.md",
        ))
        edges.append(CodeEdge(
            src=f"docs/extra{i}.md::decision-{i}",
            dst=dst,
            kind="GOVERNS",
            file_path=f"docs/extra{i}.md",
        ))
        symbols.append(_decision_symbol(
            f"docs/extra{i}.md::decision-{i}",
            f"Decision {i}",
            f"Extra decision {i}.",
        ))

    seed_index(
        state.settings,
        SLUG,
        symbols=symbols,
        edges=edges,
        vectors=[
            vector_row("docs/main.md::main-decision", axis_vec(0), text="Main decision about everything."),
        ] + [
            vector_row(f"src.mod{i}.fn", axis_vec(0), text=f"Module {i} function.")
            for i in range(5)
        ] + [
            vector_row(f"docs/extra{i}.md::decision-{i}", axis_vec(1), text=f"Extra decision {i}.")
            for i in range(5)
        ],
        fts=True,
        repo_path="/repo/test",
    )

    results = await related_decisions(state, SLUG, "main decision", k=5, repo_path="/repo/test")
    entity_results = [r for r in results if r.connected_via is not None]
    assert len(entity_results) <= 3
