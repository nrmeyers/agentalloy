"""Search endpoints (``/code/search/*``).

Rewrites the essence of codebase-indexer's ``routers/search.py`` +
``routers/symbols.py`` query surface. The old raw-Cypher passthrough is
replaced by NAMED structural queries (``/code/search/structural``) — the
graph is relational now and free-form graph queries were an injection-shaped
foot-gun anyway.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from agentalloy.code_index.api.deps import require_indexed_repo, with_handles
from agentalloy.code_index.api.models import (
    CallSiteView,
    CentralitySymbol,
    DecisionView,
    SymbolView,
)
from agentalloy.code_index.api.state import CodeIndexState, get_code_index_state
from agentalloy.code_index.retrieval.hybrid import SearchResult, lexical_search, semantic_search
from agentalloy.storage.protocols import CodeIndexHandles

router = APIRouter()

_STRUCTURAL_QUERIES = (
    "callers",
    "callees",
    "transitive_callers",
    "governing_decisions",
    "counts_by_kind",
)
_FQN_QUERIES = frozenset({"callers", "callees", "transitive_callers", "governing_decisions"})


@router.get(
    "/search/semantic",
    response_model=list[SearchResult],
    summary="Hybrid semantic search (dense + pagerank fusion + RRF/BM25)",
)
async def search_semantic(
    repo: str = Query(description="Indexed repo slug"),
    q: str = Query(min_length=1, description="Natural-language or symbol query"),
    k: int = Query(default=10, ge=1, le=100),
    state: CodeIndexState = Depends(get_code_index_state),
) -> list[SearchResult]:
    require_indexed_repo(state, repo)
    return await semantic_search(state, repo, q, k=k)


@router.get(
    "/search/lexical",
    response_model=list[SearchResult],
    summary="BM25-only lexical search over embedded symbol text",
)
async def search_lexical(
    repo: str = Query(description="Indexed repo slug"),
    q: str = Query(min_length=1),
    k: int = Query(default=10, ge=1, le=100),
    state: CodeIndexState = Depends(get_code_index_state),
) -> list[SearchResult]:
    require_indexed_repo(state, repo)
    return await lexical_search(state, repo, q, k=k)


@router.get(
    "/search/symbol",
    response_model=SymbolView,
    summary="Exact symbol lookup by fully-qualified name",
)
async def search_symbol(
    repo: str = Query(description="Indexed repo slug"),
    fqn: str = Query(min_length=1, description="Fully-qualified symbol name"),
    state: CodeIndexState = Depends(get_code_index_state),
) -> SymbolView:
    require_indexed_repo(state, repo)
    sym = await with_handles(state, repo, lambda h: h.graph.symbol(fqn))
    if sym is None:
        raise HTTPException(status_code=404, detail=f"no such symbol in {repo!r}: {fqn}")
    return SymbolView.from_symbol(sym)


@router.get(
    "/search/files",
    response_model=list[str],
    summary="List indexed file paths (optionally prefix-filtered)",
)
async def search_files(
    repo: str = Query(description="Indexed repo slug"),
    prefix: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    state: CodeIndexState = Depends(get_code_index_state),
) -> list[str]:
    require_indexed_repo(state, repo)
    return await with_handles(
        state, repo, lambda h: h.graph.list_files(prefix=prefix, limit=limit, offset=offset)
    )


@router.get(
    "/search/centrality",
    response_model=list[CentralitySymbol],
    summary="Top-pagerank symbols hydrated with their locations",
)
async def search_centrality(
    repo: str = Query(description="Indexed repo slug"),
    limit: int = Query(default=20, ge=1, le=100),
    state: CodeIndexState = Depends(get_code_index_state),
) -> list[CentralitySymbol]:
    require_indexed_repo(state, repo)

    def _collect(h: CodeIndexHandles) -> list[CentralitySymbol]:
        out: list[CentralitySymbol] = []
        for qn, pagerank in h.graph.top_centrality(limit):
            sym = h.graph.symbol(qn)
            out.append(
                CentralitySymbol(
                    qualified_name=qn,
                    pagerank=pagerank,
                    file_path=sym.file_path if sym else None,
                    start_line=sym.start_line if sym else None,
                )
            )
        return out

    return await with_handles(state, repo, _collect)


@router.get(
    "/search/structural",
    summary="Named structural graph queries (callers/callees/…)",
    responses={400: {"description": "Unknown query name or missing fqn"}},
)
async def search_structural(
    repo: str = Query(description="Indexed repo slug"),
    query: str = Query(description=f"One of: {', '.join(_STRUCTURAL_QUERIES)}"),
    fqn: str = Query(default="", description="Target symbol (required except counts_by_kind)"),
    depth: int = Query(default=4, ge=1, le=10, description="transitive_callers hop cap"),
    state: CodeIndexState = Depends(get_code_index_state),
) -> dict[str, object]:
    require_indexed_repo(state, repo)
    if query not in _STRUCTURAL_QUERIES:
        raise HTTPException(
            status_code=400,
            detail=f"unknown structural query {query!r}; valid: {', '.join(_STRUCTURAL_QUERIES)}",
        )
    if query in _FQN_QUERIES and not fqn:
        raise HTTPException(status_code=400, detail=f"structural query {query!r} requires fqn")

    def _run(h: CodeIndexHandles) -> object:
        if query == "callers":
            return [CallSiteView.from_call_site(s) for s in h.graph.callers(fqn)]
        if query == "callees":
            return [CallSiteView.from_call_site(s) for s in h.graph.callees(fqn)]
        if query == "transitive_callers":
            sites = h.graph.transitive_callers(fqn, max_depth=depth)
            return [CallSiteView.from_call_site(s) for s in sites]
        if query == "governing_decisions":
            return [DecisionView.from_decision(d) for d in h.graph.governing_decisions(fqn)]
        return h.graph.counts_by_kind()

    results = await with_handles(state, repo, _run)
    return {"query": query, "fqn": fqn or None, "results": results}
