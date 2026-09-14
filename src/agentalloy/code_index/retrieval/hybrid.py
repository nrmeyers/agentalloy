"""Hybrid code search: dense cosine + PageRank fusion + RRF with BM25.

Port of v1's ``agentalloy/code_index/retrieval/hybrid.py`` onto the shared
v2 store (one graph for all repos, no per-repo handles, sync throughout —
the v2 service handlers are sync):

1. Light descriptive-query rewrite (stop-word strip; short and symbol-like
   queries pass through untouched).
2. Embed the query (``search_query:`` prefix, same client-side cap as the
   document side).
3. Dense leg — OverGraph HNSW cosine top-N (``_FETCH_K`` candidates).
4. Centrality fusion — ``0.7*cosine + 0.3*norm(pagerank)`` over the dense
   candidates (pagerank min-max normalized within the candidate set).
5. BM25 leg — the tantivy side index (``fts.FtsIndex``); the store's GQL
   CONTAINS ``search_bm25`` stays as the fallback when no FTS exists yet.
6. Reciprocal Rank Fusion (K=60) — BM25-only hits ARE admitted.
7. Hydrate the top-k from the symbol graph (kind, snippet, repo, centrality).

A dead or absent embedder degrades ``search()`` to the lexical leg instead
of raising — the executor reports ``mode`` from
:meth:`CodeSearcher.embed_available`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel

from ..embed_client import EmbedClient, EmbedError
from ..fts import FtsIndex
from ..ingest.embed_text import MAX_EMBED_TEXT_CHARS
from ..protocols import CodeGraphStore, CodeSearchHit, CodeSymbol
from ..store import open_code_index

if TYPE_CHECKING:
    from ..api.state import CodeIndexState

QUERY_PREFIX = "search_query: "
"""Query-side task prefix (document side: ``ingest.embed_text.DOCUMENT_PREFIX``)."""

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
    symbol-like (dotted / snake_case / hyphenated token) queries."""
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
    """Apply the query prefix + the same client-side cap as documents."""
    budget = MAX_EMBED_TEXT_CHARS - len(QUERY_PREFIX)
    return QUERY_PREFIX + text[:budget]


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SearchHit:
    """One hydrated search hit (higher ``score`` = more relevant)."""

    qualified_name: str
    kind: str
    file_path: str | None
    start_line: int | None
    end_line: int | None
    score: float
    snippet: str
    repo: str = ""
    centrality: float | None = None


def _snippet(docstring: str | None, source_code: str | None) -> str:
    """Docstring first, else the first source line; capped."""
    text = (docstring or "").strip()
    if not text:
        source = (source_code or "").lstrip()
        text = source.splitlines()[0] if source else ""
    return text[:_SNIPPET_MAX_CHARS]


def _normalized_pagerank(
    dense: list[CodeSearchHit],
    pagerank: dict[str, float],
) -> dict[str, float]:
    """Min-max normalize pagerank to [0, 1] WITHIN the candidate set. A flat
    (or absent) distribution contributes 0 to every candidate."""
    values = [pagerank.get(h.qualified_name, 0.0) for h in dense]
    lo, hi = min(values, default=0.0), max(values, default=0.0)
    if hi <= lo:
        return {h.qualified_name: 0.0 for h in dense}
    return {h.qualified_name: (pagerank.get(h.qualified_name, 0.0) - lo) / (hi - lo) for h in dense}


def _rrf_fuse(dense_order: list[str], bm25_order: list[str]) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion of two ranked qualified-name lists (K=60).
    Ties break on qualified name for deterministic output."""
    fused: dict[str, float] = {}
    for order in (dense_order, bm25_order):
        for rank, qn in enumerate(order, start=1):
            fused[qn] = fused.get(qn, 0.0) + 1.0 / (_RRF_K + rank)
    return sorted(fused.items(), key=lambda item: (-item[1], item[0]))


def _hydrate(
    store: CodeGraphStore,
    ranked: list[tuple[str, float]],
    *,
    k: int,
    dense_by_qn: dict[str, CodeSearchHit],
    pagerank: dict[str, float],
) -> list[SearchHit]:
    """Build hydrated hits for the top-k ranked qualified names.

    The graph row is authoritative; a hit whose symbol row is missing (e.g.
    a stale vector row) falls back to the dense hit's denormalized location
    and is skipped entirely when neither source knows it.
    """
    out: list[SearchHit] = []
    for qn, score in ranked:
        if len(out) >= k:
            break
        sym = store.symbol(qn)
        if sym is not None:
            out.append(
                SearchHit(
                    qualified_name=qn,
                    kind=sym.kind,
                    file_path=sym.file_path,
                    start_line=sym.start_line,
                    end_line=sym.end_line,
                    score=score,
                    snippet=_snippet(sym.docstring, sym.source_code),
                    repo=sym.repo,
                    centrality=pagerank.get(qn),
                )
            )
            continue
        hit = dense_by_qn.get(qn)
        if hit is not None:
            out.append(
                SearchHit(
                    qualified_name=qn,
                    kind="unknown",
                    file_path=hit.file_path,
                    start_line=hit.start_line,
                    end_line=hit.end_line,
                    score=score,
                    snippet="",
                    centrality=pagerank.get(qn),
                )
            )
    return out


# ---------------------------------------------------------------------------
# Searcher
# ---------------------------------------------------------------------------


class CodeSearcher:
    """Hybrid retrieval bound to the shared store + FTS side index.

    One instance per service (see ``executors``); every method is sync and
    safe to call from the request handlers.
    """

    def __init__(
        self,
        store: CodeGraphStore,
        fts: FtsIndex | None,
        embed_client: EmbedClient | None,
    ) -> None:
        self._store = store
        self._fts = fts
        self._embed_client = embed_client
        self._last_mode = "lexical-only"

    @property
    def store(self) -> CodeGraphStore:
        return self._store

    @property
    def last_mode(self) -> str:
        """``"hybrid"`` or ``"lexical-only"`` for the most recent
        :meth:`search` — the mode the executor reports in the wire row."""
        return self._last_mode

    def embed_available(self) -> bool:
        """Liveness of the dense leg; False means lexical-only mode."""
        if self._embed_client is None:
            return False
        try:
            return self._embed_client.is_available()
        except EmbedError:
            return False

    def search(self, query: str, *, k: int = 10) -> list[SearchHit]:
        """Full hybrid pipeline (see module docstring); degrades to the
        lexical leg when the embedder is down."""
        query = (query or "").strip()
        if not query:
            return []
        query_vec = self._embed_query(query)
        if query_vec is None:
            self._last_mode = "lexical-only"
            return self.lexical(query, k=k)

        dense = self._store.search_similar(query_vec, k=_FETCH_K)
        self._last_mode = "hybrid" if dense else "lexical-only"
        dense_by_qn = {h.qualified_name: h for h in dense}
        pagerank = self._store.read_centrality([h.qualified_name for h in dense])

        # Centrality fusion over the dense candidates only (BM25 scores are
        # rank-fused below, not blended with pagerank).
        norm_pr = _normalized_pagerank(dense, pagerank)
        fused_dense = sorted(
            dense,
            key=lambda h: (
                -(_COSINE_WEIGHT * h.score + _PAGERANK_WEIGHT * norm_pr[h.qualified_name]),
                h.qualified_name,
            ),
        )

        bm25_order = [qn for qn, _score in self._bm25(query)]
        ranked = _rrf_fuse([h.qualified_name for h in fused_dense], bm25_order)
        return _hydrate(
            self._store,
            ranked,
            k=k,
            dense_by_qn=dense_by_qn,
            pagerank=pagerank,
        )

    def lexical(self, query: str, *, k: int = 10) -> list[SearchHit]:
        """BM25-only search, hydrated from the graph the same way."""
        query = (query or "").strip()
        if not query:
            return []
        self._last_mode = "lexical-only"
        ranked = self._bm25(query)[: max(1, k)]
        return _hydrate(self._store, ranked, k=k, dense_by_qn={}, pagerank={})

    def _embed_query(self, query: str) -> list[float] | None:
        if self._embed_client is None:
            return None
        text = finalize_query_text(rewrite_query(query))
        try:
            return self._embed_client.embed([text])[0]
        except EmbedError:
            return None

    def _bm25(self, query: str) -> list[tuple[str, float]]:
        if self._fts is not None:
            results = self._fts.search(query, k=_FETCH_K)
            if results:
                return results
        # Fallback: the store's GQL CONTAINS leg (real BM25 is the FTS above).
        return self._store.search_bm25(query, k=_FETCH_K)


# ---------------------------------------------------------------------------
# Per-slug async search surface (v1 API, used by the /code routers)
#
# The v2 ``CodeSearcher`` above is bound to ONE shared store instance and is
# sync (service request handlers). The v1 shell (search_router, bundle,
# knowledge_push) instead opens a PER-SLUG store handle per call and awaits
# the store work in a worker thread. The two paths share the rewrite /
# fusion helpers above; the v1 helpers that would collide with the v2
# names are suffixed (``_snippet_from_symbol`` / ``_hydrate_results`` /
# ``_embed_query_state``).
# ---------------------------------------------------------------------------


def _in_list(qns: list[str]) -> str:
    """Build a quoted, escaped ``('a', 'b')`` list for the store ``where`` clause."""
    return "(" + ", ".join("'" + qn.replace("'", "''") + "'" for qn in qns) + ")"


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


def _snippet_from_symbol(sym: CodeSymbol) -> str:
    """Docstring first, else the first source line; capped."""
    text = (sym.docstring or "").strip()
    if not text:
        source = (sym.source_code or "").lstrip()
        text = source.splitlines()[0] if source else ""
    return text[:_SNIPPET_MAX_CHARS]


def _hydrate_results(
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
                    snippet=_snippet_from_symbol(sym),
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


def _embed_query_state(state: CodeIndexState, query: str) -> list[float]:
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
    query_vec = await asyncio.to_thread(_embed_query_state, state, query)

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
            return _hydrate_results(
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
            return _hydrate_results(
                handles.graph, ranked, k=k, dense_by_qn={}, indexed_head=indexed_head
            )
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
    query_vec = await asyncio.to_thread(_embed_query_state, state, query)

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
            semantic = _hydrate_results(
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
