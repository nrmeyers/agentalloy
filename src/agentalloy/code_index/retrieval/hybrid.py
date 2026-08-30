"""Hybrid code search: dense cosine + PageRank fusion + RRF with BM25.

Rewrites the essence of codebase-indexer's ``services/retrieval.py``
pipeline against the agentalloy stores:

1. Light descriptive-query rewrite (stop-word strip; skip short or
   symbol-like queries).
2. Embed the query (nomic ``search_query: `` prefix, same client-side length
   cap as the document side).
3. Dense leg — OverGraph HNSW cosine top-N.
4. Centrality fusion — ``0.7 * cosine + 0.3 * normalized_pagerank`` over the
   dense candidates (pagerank min-max normalized within the candidate set).
5. BM25 leg — Tantivy BM25 over the embedded text.
6. Reciprocal Rank Fusion (K=60) of the two ranked lists. Unlike the source
   (which only reordered dense candidates), BM25-only hits ARE admitted —
   the graph store hydrates them just as well.
7. Hydrate the top-k from the symbol graph (kind / docstring snippet).

Everything store-touching runs in a worker thread: the stores and the embed
client are synchronous.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from pydantic import BaseModel

from agentalloy.code_index.ingest.embed_text import MAX_EMBED_TEXT_CHARS
from agentalloy.code_index.store import open_code_index
from agentalloy.storage.protocols import CodeGraphStore, CodeSearchHit, CodeSymbol


def _in_list(qns: list[str]) -> str:
    """Build a quoted, escaped ``('a', 'b')`` list for the store ``where`` clause."""
    return "(" + ", ".join("'" + qn.replace("'", "''") + "'" for qn in qns) + ")"


if TYPE_CHECKING:
    from agentalloy.code_index.api.state import CodeIndexState

QUERY_PREFIX = "search_query: "
"""nomic-embed-text-v1.5 query-side task prefix (document side lives in
``ingest.embed_text.DOCUMENT_PREFIX``)."""

_FETCH_K = 50
"""Per-leg candidate pool size (dense and BM25)."""

_RRF_K = 60
"""Canonical RRF constant (Cormack et al. 2009)."""

_COSINE_WEIGHT = 0.7
_PAGERANK_WEIGHT = 0.3

_SNIPPET_MAX_CHARS = 200


# ---------------------------------------------------------------------------
# Query rewrite
# ---------------------------------------------------------------------------

# Essence of codebase-indexer's ``_rewrite_descriptive_query``: descriptive
# queries phrase intent nominally while code uses verbs; English filler drags
# the query embedding toward generic prose. Strip filler on long queries,
# pass short or symbol-like queries through untouched.
_QUERY_STOP_WORDS: frozenset[str] = frozenset(
    {
        # articles / demonstratives
        "a", "an", "the", "this", "that", "these", "those",
        # prepositions
        "in", "on", "at", "to", "from", "of", "for", "with", "without",
        "into", "onto", "via", "by", "as", "about", "against", "between",
        "across", "through", "during", "before", "after",
        # conjunctions
        "and", "or", "but", "nor", "so", "yet",
        # aux verbs / question shaping
        "is", "are", "was", "were", "be", "been", "being", "do", "does",
        "did", "has", "have", "had", "can", "could", "should", "would",
        "will", "shall", "may", "might", "must",
        "how", "what", "where", "when", "why", "which", "who",
        # pronouns
        "i", "me", "my", "you", "your", "we", "us", "our", "it", "its",
        "they", "them", "their",
        # generic search verbs
        "show", "find", "list", "get",
    },
)  # fmt: skip


def rewrite_query(raw: str) -> str:
    """Strip stop-words from descriptive queries; never rewrite short or
    symbol-like (dotted / snake_case / hyphenated token) queries.
    """
    tokens = raw.rstrip("?").strip().split()
    if len(tokens) < 4:
        return raw
    if any(ch in t for t in tokens for ch in "._-"):
        return raw
    kept = [t for t in tokens if t.lower() not in _QUERY_STOP_WORDS]
    if len(kept) < 2:
        return raw
    return " ".join(kept)


def finalize_query_text(text: str) -> str:
    """Apply the nomic query prefix + the same client-side cap as documents."""
    budget = MAX_EMBED_TEXT_CHARS - len(QUERY_PREFIX)
    return QUERY_PREFIX + text[:budget]


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


class SearchResult(BaseModel):
    """One hydrated search hit (higher ``score`` = more relevant)."""

    qualified_name: str
    kind: str
    file_path: str | None
    start_line: int | None
    end_line: int | None
    score: float
    snippet: str
    indexed_head: str | None = None
    """HEAD commit this index was built from (for staleness detection)."""
    connected_via: str | None = None
    """Entity edge kind that connected this result (e.g. CONSTRAINTS, TOUCHES).
    None for semantic matches; set for entity-connected discoveries."""


def _snippet(sym: CodeSymbol) -> str:
    """Docstring first, else the first source line; capped."""
    text = (sym.docstring or "").strip()
    if not text:
        source = (sym.source_code or "").lstrip()
        text = source.splitlines()[0] if source else ""
    return text[:_SNIPPET_MAX_CHARS]


def _hydrate(
    graph: CodeGraphStore,
    ranked: list[tuple[str, float]],
    *,
    k: int,
    dense_by_qn: dict[str, CodeSearchHit],
    indexed_head: str | None = None,
) -> list[SearchResult]:
    """Build typed results for the top-k ranked qualified names.

    The graph row is authoritative; a hit whose symbol row is missing (e.g.
    a stale vector row) falls back to the dense hit's denormalized location
    and is skipped entirely when neither source knows it.
    """
    out: list[SearchResult] = []
    for qn, score in ranked:
        if len(out) >= k:
            break
        sym = graph.symbol(qn)
        if sym is not None:
            out.append(
                SearchResult(
                    qualified_name=qn,
                    kind=sym.kind,
                    file_path=sym.file_path,
                    start_line=sym.start_line,
                    end_line=sym.end_line,
                    score=score,
                    snippet=_snippet(sym),
                    indexed_head=indexed_head,
                ),
            )
            continue
        hit = dense_by_qn.get(qn)
        if hit is not None:
            out.append(
                SearchResult(
                    qualified_name=qn,
                    kind="unknown",
                    file_path=hit.file_path,
                    start_line=hit.start_line,
                    end_line=hit.end_line,
                    score=score,
                    snippet="",
                    indexed_head=indexed_head,
                ),
            )
    return out


def _normalized_pagerank(
    dense: list[CodeSearchHit],
    pagerank: dict[str, float],
) -> dict[str, float]:
    """Min-max normalize pagerank to [0, 1] WITHIN the candidate set. A flat
    (or absent) distribution contributes 0 to every candidate.
    """
    values = [pagerank.get(h.qualified_name, 0.0) for h in dense]
    lo, hi = min(values, default=0.0), max(values, default=0.0)
    if hi <= lo:
        return {h.qualified_name: 0.0 for h in dense}
    return {h.qualified_name: (pagerank.get(h.qualified_name, 0.0) - lo) / (hi - lo) for h in dense}


def _rrf_fuse(dense_order: list[str], bm25_order: list[str]) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion of two ranked qualified-name lists (K=60).
    Ties break on qualified name for deterministic output.
    """
    fused: dict[str, float] = {}
    for order in (dense_order, bm25_order):
        for rank, qn in enumerate(order, start=1):
            fused[qn] = fused.get(qn, 0.0) + 1.0 / (_RRF_K + rank)
    return sorted(fused.items(), key=lambda item: (-item[1], item[0]))


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def _embed_query(state: CodeIndexState, query: str) -> list[float]:
    text = finalize_query_text(rewrite_query(query))
    vectors = state.embed_client.embed(model=state.settings.runtime_embedding_model, texts=[text])
    return vectors[0]


async def semantic_search(
    state: CodeIndexState,
    slug: str,
    query: str,
    *,
    k: int = 10,
    repo_path: str | None = None,
    indexed_head: str | None = None,
) -> list[SearchResult]:
    """Full hybrid pipeline (see module docstring). Returns at most ``k``."""
    query_vec = await asyncio.to_thread(_embed_query, state, query)

    def _search() -> list[SearchResult]:
        handles = open_code_index(state.settings, slug, role="service", repo_path=repo_path)
        try:
            dense = handles.vectors.search_similar(query_vec, k=_FETCH_K)
            dense_by_qn = {h.qualified_name: h for h in dense}

            # Centrality fusion over the dense candidates only (BM25 scores
            # are rank-fused below, not blended with pagerank).
            norm_pr = _normalized_pagerank(
                dense,
                handles.graph.read_centrality([h.qualified_name for h in dense]),
            )
            fused_dense = sorted(
                dense,
                key=lambda h: (
                    -(_COSINE_WEIGHT * h.score + _PAGERANK_WEIGHT * norm_pr[h.qualified_name]),
                    h.qualified_name,
                ),
            )

            # BM25 leg: raw query text (Tantivy does its own tokenization).
            bm25 = handles.vectors.search_bm25(query, k=_FETCH_K)

            ranked = _rrf_fuse([h.qualified_name for h in fused_dense], [qn for qn, _score in bm25])
            return _hydrate(
                handles.graph,
                ranked,
                k=k,
                dense_by_qn=dense_by_qn,
                indexed_head=indexed_head,
            )
        finally:
            handles.close()

    return await asyncio.to_thread(_search)


async def lexical_search(
    state: CodeIndexState,
    slug: str,
    query: str,
    *,
    k: int = 10,
    repo_path: str | None = None,
    indexed_head: str | None = None,
) -> list[SearchResult]:
    """BM25-only search, hydrated from the graph the same way."""

    def _search() -> list[SearchResult]:
        handles = open_code_index(state.settings, slug, role="service", repo_path=repo_path)
        try:
            ranked = handles.vectors.search_bm25(query, k=k)
            return _hydrate(handles.graph, ranked, k=k, dense_by_qn={}, indexed_head=indexed_head)
        finally:
            handles.close()

    return await asyncio.to_thread(_search)


async def related_decisions(
    state: CodeIndexState,
    slug: str,
    query: str,
    *,
    k: int = 8,
    repo_path: str | None = None,
    indexed_head: str | None = None,
) -> list[SearchResult]:
    """Hybrid search constrained to decision-doc chunks (MarkdownDoc kind).

    Constrains both dense and BM25 legs to the decision-doc qn set so the full
    ``_FETCH_K`` candidate budget is spent on decisions. PageRank fusion is
    dropped — decision docs aren't in the call graph, so ``norm_pr`` is all-zero
    for them.
    """
    query_vec = await asyncio.to_thread(_embed_query, state, query)

    def _search() -> list[SearchResult]:
        handles = open_code_index(state.settings, slug, role="service", repo_path=repo_path)
        try:
            qns = handles.graph.decision_qns()
            if not qns:
                return []
            where = f"qualified_name IN {_in_list(qns)}"
            dense = handles.vectors.search_similar(query_vec, k=_FETCH_K, where=where)
            bm25 = handles.vectors.search_bm25(query, k=_FETCH_K, where=where)
            ranked = _rrf_fuse([d.qualified_name for d in dense], [qn for qn, _ in bm25])
            semantic = _hydrate(
                handles.graph,
                ranked,
                k=k,
                dense_by_qn={d.qualified_name: d for d in dense},
                indexed_head=indexed_head,
            )

            # Entity expansion: traverse entity edges from semantic results
            # and surface additional decisions connected via those edges.
            entity_results: list[SearchResult] = []
            seen_qns: set[str] = {r.qualified_name for r in semantic}
            chunk_qns = [r.qualified_name for r in semantic]
            entity_edges = handles.graph.typed_edges_from_chunks(chunk_qns, limit=20)
            for edge in entity_edges:
                if len(entity_results) >= 3:
                    break
                dst = edge.dst
                if not dst:
                    continue
                governing = handles.graph.governing_decisions(dst)
                for d in governing:
                    if d.qualified_name in seen_qns:
                        continue
                    if len(entity_results) >= 3:
                        break
                    entity_results.append(
                        SearchResult(
                            qualified_name=d.qualified_name,
                            kind="MarkdownDoc",
                            file_path=d.file_path,
                            start_line=d.start_line,
                            end_line=None,
                            score=0.0,
                            snippet=d.snippet or "",
                            indexed_head=indexed_head,
                            connected_via=edge.kind,
                        ),
                    )
                    seen_qns.add(d.qualified_name)

            return semantic + entity_results
        finally:
            handles.close()

    return await asyncio.to_thread(_search)
