"""Storage protocols, DTOs, and shared constants for the two-engine backend.

v5 splits the old monolithic ``VectorStore`` (DuckDB-vss + DuckDB-fts + telemetry
+ corpus_meta) and the legacy graph store into three focused stores:

- ``FragmentStore``  — LanceDB ``fragments`` dataset: vector ANN (retrieval) +
  exact-cosine (dedup) + native BM25. Derived index, rebuilt from the SQL source.
- ``SkillStore``     — DuckDB ``agentalloy.duck``: skill metadata (folded out of
  the legacy graph) + ``corpus_meta`` kv. Source of truth for fragment content/metadata.
- ``TelemetryStore`` — DuckDB ``telemetry.duck``: ``composition_traces`` only,
  service-owned so runtime writes never contend with the reembed writer.

The DTOs and ``EMBEDDING_DIM`` / ``EmbeddingDimMismatch`` / ``l2_normalize``
live here as the single canonical home; callers import from
``agentalloy.storage`` (re-exported) or from this module directly.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

EMBEDDING_DIM = 768
"""Vector dimensionality. Tied to ``nomic-embed-text-v1.5`` (768-dim). Fixed:
the Lance ``embedding`` column is ``FixedSizeList(float32, 768)`` and the gate
chain (pack manifest, corpus-stamp, doctor, health) enforces it everywhere."""


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class VectorStoreError(Exception):
    """Base for storage errors."""


class EmbeddingDimMismatch(VectorStoreError):
    """Raised when an embedding's length doesn't match ``EMBEDDING_DIM``.

    The message MUST contain one of the substrings ``upgrade.py`` greps for
    (``embedding_dim`` / ``EmbeddingDimMismatch`` / ``dimension`` /
    ``-dim embeddings``) so the self-heal re-embed path still fires.
    """


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def l2_normalize(vec: Sequence[float]) -> list[float]:
    """Return the L2-normalized form of ``vec`` (unit Euclidean norm).

    Raises ``ValueError`` if ``vec`` is the zero vector (no defined direction).
    Retained as a pre-write step so cosine distance == 1 - cosine_similarity and
    the dedup thresholds (0.92 / 0.80) keep their meaning (decision D2).
    """
    norm_sq = sum(x * x for x in vec)
    if norm_sq == 0.0:
        raise ValueError("cannot L2-normalize the zero vector")
    norm = math.sqrt(norm_sq)
    return [x / norm for x in vec]


# ---------------------------------------------------------------------------
# DTOs (identical shapes to the v5.3 vector_store DTOs)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FragmentEmbedding:
    """A fragment's embedding plus the denormalized columns that make filtered
    vector search cheap. In v5 these columns are a *derived projection* of the
    SQL-canonical source (``agentalloy.duck``), rebuilt on every reembed, so
    they cannot drift (decision D8: always consistent)."""

    fragment_id: str
    embedding: Sequence[float]  # raw; normalized on insert
    skill_id: str
    category: str
    fragment_type: str
    embedded_at: int  # unix epoch seconds
    embedding_model: str
    prose: str = ""  # raw fragment text; indexed for BM25
    phase_scope: tuple[str, ...] | None = None


@dataclass(frozen=True)
class SimilarityHit:
    fragment_id: str
    skill_id: str
    distance: float  # cosine distance in [0, 2]; 0 = identical direction


@dataclass(frozen=True)
class BM25Hit:
    fragment_id: str
    score: float  # BM25 score; higher = more relevant


@dataclass(frozen=True)
class CompositionTrace:
    """One row in ``composition_traces``. Optional fields carry None into the
    DB column as SQL NULL."""

    trace_id: str
    request_ts: int
    phase: str
    task_prompt: str
    status: str
    correlation_id: str | None = None
    category: str | None = None
    repo: str | None = None
    session_key: str | None = None
    session_source: str | None = None
    selected_fragment_ids: list[str] = field(default_factory=lambda: [])
    source_skill_ids: list[str] = field(default_factory=lambda: [])
    system_skill_ids: list[str] = field(default_factory=lambda: [])
    assembly_tier: str | None = None
    assembly_model: str | None = None
    retrieval_latency_ms: int | None = None
    assembly_latency_ms: int | None = None
    total_latency_ms: int | None = None
    error_code: str | None = None
    response_size_chars: int | None = None
    prompt_version: str | None = None
    workflow_skill_ids: list[str] = field(default_factory=lambda: [])
    contract_path: str | None = None
    contract_tags: list[str] = field(default_factory=lambda: [])
    bm25_source: str = "rule-extracted"  # "rule-extracted" | "contract" | "union"
    event_type: str = "compose"  # "compose" | "proxy_request"
    pre_filter_matched: str | None = None
    gates_met: list[str] = field(default_factory=lambda: list[str]())
    gates_unmet: list[str] = field(default_factory=lambda: list[str]())
    qwen_calls: int = 0
    reranked: bool = False
    tokens_returned: int = 0
    tokens_flat_equivalent: int = 0
    lm_assist_outcome: str = "disabled"  # "disabled" | "hit" | "timeout" | "error"
    lm_assist_model: str | None = None
    dense_leg_degraded: bool = False
    phase_gate_embed_failed: bool = False
    lm_assist_kept_ids: list[str] = field(default_factory=lambda: list[str]())
    lm_assist_dropped_ids: list[str] = field(default_factory=lambda: list[str]())
    lm_assist_scores: str | None = None


# ---------------------------------------------------------------------------
# Code-index DTOs (per-repo symbol graph + vector index; see
# ``agentalloy.code_index.store``)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CodeSymbol:
    """One code symbol row in the per-repo DuckDB graph.

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


@dataclass(frozen=True)
class CodeEdge:
    """One relationship row (CALLS / CONTAINS / IMPORTS / ...) between two
    qualified names. Endpoints may dangle (unresolved externals) — no FKs."""

    src: str
    dst: str
    kind: str
    file_path: str = ""
    line_start: int = 0
    col_start: int = 0
    resolved_via: str = "unknown"
    confidence: float = 1.0
    new_target: str = ""


@dataclass(frozen=True)
class CodeVectorRow:
    """A symbol's embedding plus the denormalized columns the search surface
    returns. Derived from the graph store; rebuilt on re-embed."""

    qualified_name: str
    embedding: Sequence[float]  # raw; normalized on insert
    symbol_type: str
    file_path: str
    start_line: int | None
    end_line: int | None
    text: str  # embedded text (contextual prefix + source); indexed for BM25
    indexed_at: int  # unix epoch seconds


@dataclass(frozen=True)
class CallSite:
    """One caller/callee hit for the symbol-relations query surface."""

    qualified_name: str
    file_path: str | None
    line: int | None


@dataclass(frozen=True)
class CodeSearchHit:
    """One vector/FTS search hit. ``score`` is higher-is-better (cosine
    similarity for the dense leg, BM25 for the sparse leg)."""

    qualified_name: str
    file_path: str
    start_line: int | None
    end_line: int | None
    score: float


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class FragmentStore(Protocol):
    """Vector + BM25 index over fragments (LanceDB). Derived from SkillStore."""

    def insert_embeddings(self, items: Iterable[FragmentEmbedding]) -> int: ...
    def search_similar(
        self,
        query_vec: Sequence[float],
        *,
        categories: list[str] | None = None,
        phases: list[str] | None = None,
        fragment_types: list[str] | None = None,
        deprecated_skill_ids: list[str] | None = None,
        k: int = 10,
    ) -> list[SimilarityHit]: ...
    def search_bm25(
        self,
        query: str,
        *,
        categories: list[str] | None = None,
        phases: list[str] | None = None,
        deprecated_skill_ids: list[str] | None = None,
        k: int = 10,
    ) -> list[BM25Hit]: ...
    def backfill_phase_scope(self, scope_by_skill: dict[str, list[str] | None]) -> int: ...
    def count_embeddings(self) -> int: ...
    def count_cards(self) -> int: ...
    def delete_cards(self, skill_id: str | None = None) -> int: ...
    def delete_skill(self, skill_id: str) -> int: ...
    def delete_all(self) -> int: ...
    def embedding_dim(self) -> int | None: ...
    def fragment_ids_present(self, fragment_ids: Sequence[str]) -> set[str]: ...
    def rebuild_fts_index(self) -> None: ...
    def close(self) -> None: ...


@runtime_checkable
class SkillStore(Protocol):
    """Skill metadata + corpus_meta (DuckDB ``agentalloy.duck``)."""

    def migrate(self) -> None: ...
    def execute(
        self, sql: str, params: Sequence[object] | Mapping[str, object] | None = None
    ) -> list[tuple[Any, ...]]: ...
    def scalar(
        self, sql: str, params: Sequence[object] | Mapping[str, object] | None = None
    ) -> Any: ...
    def begin(self) -> None: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def delete_skill(self, skill_id: str) -> int: ...
    def rollback_skill(self, skill_id: str) -> None: ...
    def rollback_batch(self, skill_ids: Sequence[str]) -> None: ...
    def set_meta(self, key: str, value: str) -> None: ...
    def get_meta(self, key: str) -> str | None: ...
    def close(self) -> None: ...


@runtime_checkable
class TelemetryStore(Protocol):
    """Composition traces (DuckDB ``telemetry.duck``)."""

    def record_composition_trace(self, trace: CompositionTrace) -> None: ...
    def count_traces(self) -> int: ...
    def count_traces_filtered(
        self,
        *,
        phase: str | None = None,
        status: str | None = None,
        since: int | None = None,
        until: int | None = None,
        repo: str | None = None,
    ) -> int: ...
    def query_traces(
        self,
        *,
        phase: str | None = None,
        status: str | None = None,
        since: int | None = None,
        until: int | None = None,
        repo: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[CompositionTrace]: ...
    def aggregate_savings(self, repo: str | None = None) -> dict[str, object]: ...
    def aggregate_coverage(self, repo: str | None = None) -> dict[str, object]: ...
    def clear_telemetry(self) -> dict[str, int]: ...
    def close(self) -> None: ...


@runtime_checkable
class CodeGraphStore(Protocol):
    """Per-repo symbol graph (DuckDB ``graph.duck``). Source of truth for the
    code index; the Lance vector dataset is derived from it."""

    def migrate(self) -> None: ...
    def replace_all(
        self, symbols: Iterable[CodeSymbol], edges: Iterable[CodeEdge]
    ) -> tuple[int, int]: ...
    def upsert_symbols(self, symbols: Iterable[CodeSymbol]) -> int: ...
    def upsert_edges(self, edges: Iterable[CodeEdge]) -> int: ...
    def delete_for_files(self, file_paths: Sequence[str]) -> int: ...
    def symbol(self, qualified_name: str) -> CodeSymbol | None: ...
    def callers(self, fqn: str) -> list[CallSite]: ...
    def callees(self, fqn: str) -> list[CallSite]: ...
    def transitive_callers(self, fqn: str, *, max_depth: int = 4) -> list[CallSite]: ...
    def counts_by_kind(self) -> dict[str, int]: ...
    def list_files(
        self, *, prefix: str | None = None, limit: int = 100, offset: int = 0
    ) -> list[str]: ...
    def calls_edges(self) -> list[tuple[str, str]]: ...
    def write_centrality(self, scores: Mapping[str, float]) -> int: ...
    def read_centrality(self, qualified_names: Sequence[str]) -> dict[str, float]: ...
    def top_centrality(self, limit: int = 20) -> list[tuple[str, float]]: ...
    def content_hashes(self) -> dict[str, str]: ...
    def set_meta(self, key: str, value: str) -> None: ...
    def get_meta(self, key: str) -> str | None: ...
    def close(self) -> None: ...


@runtime_checkable
class CodeVectorStore(Protocol):
    """Per-repo vector ANN + BM25 over symbols (LanceDB ``vectors.lance``)."""

    def upsert(self, rows: Iterable[CodeVectorRow]) -> int: ...
    def bulk_replace(self, rows: Iterable[CodeVectorRow]) -> int: ...
    def search_similar(self, query_vec: Sequence[float], *, k: int = 10) -> list[CodeSearchHit]: ...
    def search_bm25(self, query: str, *, k: int = 10) -> list[tuple[str, float]]: ...
    def delete(self, qualified_names: Sequence[str]) -> int: ...
    def count(self) -> int: ...
    def rebuild_fts_index(self) -> None: ...
    def embedding_dim(self) -> int | None: ...
    def close(self) -> None: ...


@dataclass
class CodeIndexHandles:
    """Bundle returned by ``code_index.store.open.open_code_index``."""

    slug: str
    graph: CodeGraphStore
    vectors: CodeVectorStore

    def close(self) -> None:
        import contextlib

        for s in (self.graph, self.vectors):
            with contextlib.suppress(Exception):
                s.close()


@dataclass
class Stores:
    """Bundle returned by ``open_stores``. Callers request only what they need."""

    fragments: FragmentStore
    skills: SkillStore
    telemetry: TelemetryStore

    def close(self) -> None:
        import contextlib

        for s in (self.fragments, self.skills, self.telemetry):
            with contextlib.suppress(Exception):
                s.close()


__all__ = [
    "EMBEDDING_DIM",
    "VectorStoreError",
    "EmbeddingDimMismatch",
    "l2_normalize",
    "FragmentEmbedding",
    "SimilarityHit",
    "BM25Hit",
    "CompositionTrace",
    "FragmentStore",
    "SkillStore",
    "TelemetryStore",
    "Stores",
    "CodeSymbol",
    "CodeEdge",
    "CodeVectorRow",
    "CallSite",
    "CodeSearchHit",
    "CodeGraphStore",
    "CodeVectorStore",
    "CodeIndexHandles",
]
