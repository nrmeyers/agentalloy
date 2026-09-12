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

from dataclasses import dataclass

from ..embed_client import EmbedClient, EmbedError
from ..fts import FtsIndex
from ..ingest.embed_text import MAX_EMBED_TEXT_CHARS
from ..protocols import CodeGraphStore, CodeSearchHit

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
