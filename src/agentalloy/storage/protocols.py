"""Storage protocols, DTOs, and shared constants.

The corpus lives in ONE unified OverGraph store (``agentalloy.overgraph`` +
its Tantivy BM25 sidecar), exposed through two protocol views:

- ``SkillStore``     — skill metadata (folded out of the legacy graph) +
  ``corpus_meta`` kv. Source of truth for fragment content/metadata.
- ``FragmentStore``  — vector ANN (retrieval) + exact-cosine (dedup) + BM25
  over the same store. Derived index, rebuilt from the canonical rows on
  every reembed.

``OverGraphSkillStore`` implements both, so one read-only handle serves the
whole app (``open_skills``). Separately:

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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol, runtime_checkable

EMBEDDING_DIM = 768
"""Vector dimensionality. Tied to ``nomic-embed-text-v1.5`` (768-dim). Fixed:
the OverGraph ``embedding`` attribute is a 768-float vector and the gate
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


class LockHeldError(Exception):
    """Another process holds the corpus store's single writer lock."""


def is_lock_held_error(text: str) -> bool:
    """True if ``text`` looks like a corpus-store writer-lock conflict.

    Matches the OverGraph/Tantivy ``LockBusy`` failure raised when a second
    writer opens the store (its BM25 sidecar takes an exclusive index lock).
    """
    t = text.lower()
    return (
        "lockbusy" in t or "failed to acquire lockfile" in t or "failed to acquire index lock" in t
    )


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
    vector search cheap. These columns are a *derived projection* of the
    canonical fragment rows, rebuilt on every reembed, so they cannot drift
    (decision D8: always consistent).
    """

    fragment_id: str
    embedding: Sequence[float]  # raw; normalized on insert
    skill_id: str
    category: str
    fragment_type: str
    embedded_at: int  # unix epoch seconds
    embedding_model: str
    prose: str = ""  # raw fragment text; indexed for BM25
    phase_scope: tuple[str, ...] | None = None
    domain_tags: tuple[str, ...] | None = None


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
    DB column as SQL NULL.
    """

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
    contract_id: str | None = None
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
    """One code symbol row in the per-repo code graph.

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
    # Provenance for GOVERNS edges (#527 C): the fenced span that resolved to
    # ``dst`` and the resolution tier (1 = exact fqn, 2 = unique short-name).
    # None for non-GOVERNS edges (CALLS/IMPORTS/... never populate these).
    span: str | None = None
    resolution_tier: int | None = None


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
    text: str  # embedded text (contextual prefix + source); indexed for BM25
    indexed_at: int  # unix epoch seconds


@dataclass(frozen=True)
class CallSite:
    """One caller/callee hit for the symbol-relations query surface."""

    qualified_name: str
    file_path: str | None
    line: int | None


@dataclass(frozen=True)
class DecisionRow:
    """One decision governing a queried symbol (Knowledge module).

    The decision is a ``MarkdownDoc`` heading-chunk (``qualified_name`` =
    ``path::anchor``); ``heading`` is the chunk's heading and ``snippet`` its
    body. Distinct from :class:`CallSite` — a decision's ``start_line`` is a
    heading offset, and the heading/snippet have no home in the call-site view.
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
class FragmentStore(Protocol):
    """Vector + BM25 index over fragments. Derived from SkillStore."""

    def insert_embeddings(self, items: Iterable[FragmentEmbedding]) -> int: ...
    def bulk_replace(self, items: Iterable[FragmentEmbedding]) -> int: ...
    def search_similar(
        self,
        query_vec: Sequence[float],
        *,
        categories: list[str] | None = None,
        phases: list[str] | None = None,
        fragment_types: list[str] | None = None,
        deprecated_skill_ids: list[str] | None = None,
        domain_tags: list[str] | None = None,
        k: int = 10,
    ) -> list[SimilarityHit]: ...
    def search_bm25(
        self,
        query: str,
        *,
        categories: list[str] | None = None,
        phases: list[str] | None = None,
        deprecated_skill_ids: list[str] | None = None,
        domain_tags: list[str] | None = None,
        k: int = 10,
    ) -> list[BM25Hit]: ...
    def backfill_phase_scope(self, scope_by_skill: dict[str, list[str] | None]) -> int: ...
    def count_embeddings(self) -> int: ...
    def count_cards(self) -> int: ...
    def delete_cards(self, skill_id: str | None = None) -> int: ...
    def delete_skill_fragments(self, skill_id: str) -> int: ...
    def delete_all(self) -> int: ...
    def embedding_dim(self) -> int | None: ...
    def fragment_ids_present(self, fragment_ids: Sequence[str]) -> set[str]: ...
    def rebuild_fts_index(self) -> None: ...
    def close(self) -> None: ...


# ---------------------------------------------------------------------------
# SkillStore DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SkillRow:
    """A skill metadata row."""

    skill_id: str
    canonical_name: str
    category: str
    skill_class: str  # "domain" | "system" | "workflow"
    domain_tags: list[str]
    deprecated: bool
    superseded_by: str | None
    always_apply: bool
    phase_scope: list[str] | None
    category_scope: list[str] | None
    tier: str | None
    description: str | None
    current_version_id: str


@dataclass(frozen=True)
class SkillVersionRow:
    """A skill version row."""

    version_id: str
    skill_id: str
    version_number: int
    authored_at: datetime
    author: str
    change_summary: str
    status: str  # "active" | "draft" | "archived"
    raw_prose: str


@dataclass(frozen=True)
class FragmentRow:
    """A fragment row (content slice of a skill version)."""

    fragment_id: str
    version_id: str
    fragment_type: str  # "setup" | "execution" | "verification" | "example" | "guardrail" | "rationale" | "card"
    sequence: int
    content: str
    # Denormalized from parent skill for retrieval convenience
    skill_id: str = ""
    category: str = ""
    skill_class: str = ""
    domain_tags: list[str] = field(default_factory=list)
    phase_scope: list[str] | None = None
    category_scope: list[str] | None = None
    description: str | None = None


@dataclass(frozen=True)
class SkillDependencyRow:
    """A skill dependency edge."""

    source_skill_id: str
    target_skill_id: str
    rel_type: str  # "requires"


@dataclass(frozen=True)
class FragmentDiscoveryRow:
    """Fragment + parent skill metadata for the re-embed pipeline."""

    fragment_id: str
    content: str
    fragment_type: str
    skill_id: str
    category: str
    canonical_name: str
    domain_tags: tuple[str, ...]
    description: str | None


# ---------------------------------------------------------------------------
# SkillStore Protocol (higher-level, no raw SQL)
# ---------------------------------------------------------------------------


@runtime_checkable
class SkillStore(Protocol):
    """Skill metadata + fragments + corpus_meta.

    Higher-level interface — no raw SQL. The production implementation is
    the OverGraph unified store.
    """

    # --- Lifecycle ---
    def migrate(self) -> None: ...
    def close(self) -> None: ...

    # --- Transactions ---
    def begin(self) -> None: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...

    # --- Skill CRUD ---
    def get_skill(self, skill_id: str) -> SkillRow | None: ...
    def get_skill_id_by_name(self, canonical_name: str) -> str | None: ...
    def insert_skill(self, skill: SkillRow) -> None: ...
    def delete_skill(self, skill_id: str) -> int: ...
    def rollback_skill(self, skill_id: str) -> None: ...
    def rollback_batch(self, skill_ids: Sequence[str]) -> None: ...

    # --- Version CRUD ---
    def get_version(self, version_id: str) -> SkillVersionRow | None: ...
    def get_versions_by_skill(self, skill_id: str) -> list[SkillVersionRow]: ...
    def insert_version(self, version: SkillVersionRow) -> None: ...

    # --- Fragment CRUD ---
    def get_fragment(self, fragment_id: str) -> FragmentRow | None: ...
    def insert_fragment(self, fragment: FragmentRow) -> None: ...
    def count_fragments(self) -> int: ...

    # --- Dependency CRUD ---
    def get_dependencies(self, skill_id: str) -> list[SkillDependencyRow]: ...
    def insert_dependency(self, dep: SkillDependencyRow) -> None: ...
    def delete_dependencies(self, skill_id: str, rel_type: str | None = None) -> int: ...

    # --- Active-version reads (for compose/retrieval) ---
    def get_active_skills(
        self,
        *,
        skill_class: str | tuple[str, ...] | None = None,
    ) -> list[SkillRow]: ...
    def get_active_skill_by_id(self, skill_id: str) -> SkillRow | None: ...
    def get_deprecated_skill_ids(self) -> list[str]: ...
    def count_skills(self) -> int: ...
    def get_active_fragments(
        self,
        *,
        skill_class: str | tuple[str, ...] | None = None,
        categories: list[str] | None = None,
        phases: list[str] | None = None,
        domain_tags: list[str] | None = None,
    ) -> list[FragmentRow]: ...
    def get_active_fragments_for_skill(self, skill_id: str) -> list[FragmentRow]: ...

    # --- Re-embed pipeline ---
    def discover_fragments(
        self,
        *,
        skill_id: str | None = None,
    ) -> list[FragmentDiscoveryRow]: ...

    # --- Consistency guards ---
    def check_consistency(
        self,
        *,
        skill_class: str | tuple[str, ...] | None = None,
    ) -> None: ...
    def check_consistency_for(self, skill_id: str) -> None: ...

    # --- Corpus metadata KV ---
    def set_meta(self, key: str, value: str) -> None: ...
    def get_meta(self, key: str) -> str | None: ...

    # --- Bulk operations (for fixtures/tests) ---
    def clear_all(self) -> None: ...


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
    def execute(self, sql: str, params: Sequence[object] | None = None) -> None: ...
    def close(self) -> None: ...


@runtime_checkable
class CodeGraphStore(Protocol):
    """Per-repo symbol graph (``graph.overgraph``). Source of truth for the
    code index; the vector index is derived from it.
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
    def symbol(self, qualified_name: str) -> CodeSymbol | None: ...
    def callers(self, fqn: str) -> list[CallSite]: ...
    def callees(self, fqn: str) -> list[CallSite]: ...
    def transitive_callers(self, fqn: str, *, max_depth: int = 4) -> list[CallSite]: ...
    def symbols_by_name(self, name: str) -> list[tuple[str, str]]: ...
    def symbols_by_file(self, file_path: str) -> list[tuple[str, str]]: ...
    def decision_qns(self) -> list[str]: ...
    def governing_decisions(self, fqn: str) -> list[DecisionRow]: ...
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
    def content_hashes(self) -> dict[str, str]: ...
    def set_meta(self, key: str, value: str) -> None: ...
    def get_meta(self, key: str) -> str | None: ...
    def close(self) -> None: ...


@runtime_checkable
class CodeVectorStore(Protocol):
    """Per-repo vector ANN + BM25 over symbols (served by the code graph store)."""

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
class LeaseConflict:
    owner: str | None
    lease_expires_at: datetime | None
    message: str


@dataclass
class StateWriteResult:
    success: bool
    kind: str
    value: str
    owner: str | None
    lease_expires_at: datetime | None
    conflict: LeaseConflict | None


@dataclass
class LeaseResult:
    acquired: bool
    owner: str | None
    lease_expires_at: datetime | None
    conflict: LeaseConflict | None


@runtime_checkable
class StateStore(Protocol):
    """SDD state store — session-aware replacement for per-repo files."""

    def read(self, kind: str, session_key: str | None = None) -> str | None: ...
    def write(
        self,
        kind: str,
        value: str,
        *,
        session_key: str | None = None,
        owner: str | None = None,
    ) -> StateWriteResult: ...
    def acquire_lease(
        self,
        kind: str,
        session_key: str,
        duration: timedelta = timedelta(minutes=5),
    ) -> LeaseResult: ...
    def release_lease(self, kind: str, session_key: str) -> None: ...
    def import_from_files(self, agentalloy_dir: Path) -> dict[str, str]: ...
    def close(self) -> None: ...


__all__ = [
    "EMBEDDING_DIM",
    "VectorStoreError",
    "EmbeddingDimMismatch",
    "LockHeldError",
    "is_lock_held_error",
    "l2_normalize",
    "FragmentEmbedding",
    "SimilarityHit",
    "BM25Hit",
    "CompositionTrace",
    "FragmentStore",
    "SkillStore",
    "TelemetryStore",
    "StateStore",
    "StateWriteResult",
    "LeaseResult",
    "LeaseConflict",
    "CodeSymbol",
    "CodeEdge",
    "CodeVectorRow",
    "CallSite",
    "CodeSearchHit",
    "CodeGraphStore",
    "CodeVectorStore",
    "CodeIndexHandles",
]
