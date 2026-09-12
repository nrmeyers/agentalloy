"""Storage contracts for the code index (vendored from AgentAlloy v1).

DTOs + protocols the OverGraph store and retrieval layer agree on. The
v2 service has ONE shared store (multi-repo graph, ``repo`` property on
nodes) instead of v1's per-repo pair, but the row shapes and method
surface are carried over unchanged.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

EMBEDDING_DIM = 768
"""Vector dimensionality. Tied to ``nomic-embed-text-v1.5`` (768-dim).
The OverGraph DB binds this at creation; a model swap to a different
dimension requires a fresh index directory (checked at open)."""


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class VectorStoreError(Exception):
    """Base for storage errors."""


class EmbeddingDimMismatchError(VectorStoreError):
    """Raised when an embedding's length doesn't match ``EMBEDDING_DIM``."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def l2_normalize(vec: Sequence[float]) -> list[float]:
    """Return the L2-normalized form of ``vec`` (unit Euclidean norm).

    Raises ``ValueError`` if ``vec`` is the zero vector (no defined direction).
    Retained as a pre-write step so cosine distance == 1 - cosine_similarity.
    """
    norm_sq = sum(x * x for x in vec)
    if norm_sq == 0.0:
        raise ValueError("cannot L2-normalize the zero vector")
    norm = math.sqrt(norm_sq)
    return [x / norm for x in vec]


# ---------------------------------------------------------------------------
# Code-index DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CodeSymbol:
    """One code symbol row in the code graph.

    Field names line up with ``code_index.facade.ParsedSymbol`` so ingest is a
    plain field-copy; ``contextual_prefix`` / ``content_hash`` are storage-side
    enrichments (embedding context, incremental-reindex change detection).
    """

    qualified_name: str
    kind: str
    name: str
    file_path: str | None
    start_line: int | None
    end_line: int | None
    docstring: str | None
    decorators: list[str]
    is_exported: bool | None
    is_async: bool
    is_generator: bool
    source_code: str | None
    contextual_prefix: str = ""
    content_hash: str | None = None
    # Repo name (registry key) this symbol was parsed from. Populated by the
    # ingest pipeline for the shared multi-repo DB; "" for legacy rows.
    repo: str = ""


@dataclass(frozen=True)
class CodeEdge:
    """One relationship row (CALLS / CONTAINS / IMPORTS / ...) between two
    qualified names. Endpoints may dangle (unresolved externals) — no FKs.
    """

    src: str
    dst: str
    kind: str
    file_path: str = ""
    line_start: int = 0
    col_start: int = 0
    resolved_via: str = "unknown"
    confidence: float = 1.0
    new_target: str = ""
    # Provenance for GOVERNS edges: the fenced span that resolved to ``dst``
    # and the resolution tier (1 = exact fqn, 2 = unique short-name).
    # None for non-GOVERNS edges (CALLS/IMPORTS/... never populate these).
    span: str | None = None
    resolution_tier: int | None = None
    # Repo name this edge was observed in (shared multi-repo DB).
    repo: str = ""


@dataclass(frozen=True)
class CodeVectorRow:
    """A symbol's embedding plus the denormalized columns the search surface
    returns. Derived from the graph store; rebuilt on re-embed.
    """

    qualified_name: str
    embedding: Sequence[float]  # raw; normalized on insert
    symbol_type: str
    file_path: str
    start_line: int | None
    end_line: int | None
    text: str  # embedded text; indexed for BM25
    indexed_at: int  # unix epoch seconds


@dataclass(frozen=True)
class CallSite:
    """One caller/callee hit for the symbol-relations query surface."""

    qualified_name: str
    file_path: str | None
    line: int | None


@dataclass(frozen=True)
class DecisionRow:
    """One decision governing a queried symbol (knowledge tools).

    The decision is a ``MarkdownDoc`` heading-chunk (``qualified_name`` =
    ``path::anchor``); ``heading`` is the chunk's heading and ``snippet`` its
    body.
    """

    qualified_name: str
    file_path: str | None
    start_line: int | None
    heading: str
    snippet: str | None


@dataclass(frozen=True)
class CodeSearchHit:
    """One vector/FTS search hit. ``score`` is higher-is-better (cosine
    similarity for the dense leg, BM25 for the sparse leg).
    """

    qualified_name: str
    file_path: str
    start_line: int | None
    end_line: int | None
    score: float


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class CodeGraphStore(Protocol):
    """Symbol graph. Source of truth for the code index; the vector index is
    derived from it.
    """

    def migrate(self) -> None: ...
    def replace_all(
        self,
        symbols: Iterable[CodeSymbol],
        edges: Iterable[CodeEdge],
    ) -> tuple[int, int]: ...
    def upsert_symbols(self, symbols: Iterable[CodeSymbol]) -> int: ...
    def upsert_edges(self, edges: Iterable[CodeEdge]) -> int: ...
    def delete_for_files(self, file_paths: Sequence[str]) -> int: ...
    def delete_for_repo(self, repo: str) -> int: ...
    def repo_symbol_count(self, repo: str) -> int: ...
    def symbol(self, qualified_name: str) -> CodeSymbol | None: ...
    def callers(self, fqn: str) -> list[CallSite]: ...
    def callees(self, fqn: str) -> list[CallSite]: ...
    def transitive_callers(self, fqn: str, *, max_depth: int = 4) -> list[CallSite]: ...
    def symbols_by_name(self, name: str) -> list[tuple[str, str]]: ...
    def symbols_by_file(self, file_path: str) -> list[tuple[str, str]]: ...
    def symbols_matching(self, pattern: str, *, limit: int = 500) -> list[CodeSymbol]: ...
    def decision_qns(self) -> list[str]: ...
    def governing_decisions(self, fqn: str) -> list[DecisionRow]: ...
    def governs_edges_for_symbol(self, fqn: str) -> list[CodeEdge]: ...
    def governs_edges_from(self, fqn: str) -> list[CodeEdge]: ...
    def decisions_for_files(self, file_paths: Sequence[str]) -> list[DecisionRow]: ...
    def decision_docs_governing(self, fqns: Sequence[str]) -> list[str]: ...
    def delete_govern_edges_for_doc(self, doc_path: str) -> int: ...
    def delete_entity_edges_for_docs(self, file_paths: Sequence[str]) -> int: ...
    def count_govern_edges_for_doc(self, doc_path: str) -> int: ...
    def typed_edges_for_fqn(self, fqn: str) -> list[CodeEdge]: ...
    def typed_edges_from_chunks(
        self, chunk_qns: Sequence[str], *, limit: int = 20
    ) -> list[CodeEdge]: ...
    def counts_by_kind(self) -> dict[str, int]: ...
    def list_files(
        self,
        *,
        prefix: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[str]: ...
    def calls_edges(self) -> list[tuple[str, str]]: ...
    def write_centrality(self, scores: Mapping[str, float]) -> int: ...
    def read_centrality(self, qualified_names: Sequence[str]) -> dict[str, float]: ...
    def top_centrality(self, limit: int = 20) -> list[tuple[str, float]]: ...
    def subgraph(
        self,
        seeds: Sequence[str],
        *,
        hops: int = 1,
        limit: int = 20,
        repo: str | None = None,
    ) -> tuple[list[dict], list[dict]]: ...
    def content_hashes(self) -> dict[str, str]: ...
    def restore_vector_membership(self, qns: Sequence[str]) -> int: ...
    def fts_docs(self) -> list[tuple[str, str]]: ...
    def set_meta(self, key: str, value: str) -> None: ...
    def get_meta(self, key: str) -> str | None: ...
    def close(self) -> None: ...


@runtime_checkable
class CodeVectorStore(Protocol):
    """Vector ANN + BM25 over symbols (served by the code graph store)."""

    def upsert(self, rows: Iterable[CodeVectorRow]) -> int: ...
    def bulk_replace(self, rows: Iterable[CodeVectorRow]) -> int: ...
    def search_similar(
        self,
        query_vec: Sequence[float],
        *,
        k: int = 10,
        where: str | None = None,
    ) -> list[CodeSearchHit]: ...
    def search_bm25(
        self,
        query: str,
        *,
        k: int = 10,
        where: str | None = None,
    ) -> list[tuple[str, float]]: ...
    def delete(self, qualified_names: Sequence[str]) -> int: ...
    def count(self) -> int: ...
    def rebuild_fts_index(self) -> None: ...
    def embedding_dim(self) -> int | None: ...
    def close(self) -> None: ...
