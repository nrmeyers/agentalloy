"""Pydantic request/response models for the ``/code`` surface.

Import-light on purpose: this module must not pull in the tree-sitter engine
(only the jobs-store DTOs), so OpenAPI generation and tests can use the
models without the ``[code-index]`` extra's heavy imports.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from agentalloy.code_index.store import CodeIndexJob, IndexedRepo
from agentalloy.storage.protocols import CallSite, CodeEdge, CodeSymbol, DecisionRow


class IndexRequest(BaseModel):
    """POST /code/index body."""

    repo_path: str = Field(description="Absolute path to the repository to index.")
    force: bool = Field(default=False, description="Full rebuild: ignore stored content hashes.")
    index_markdown: bool = Field(default=True, description="Also chunk + embed markdown docs.")
    prune_decisions: bool = Field(
        default=False,
        description=(
            "Escape hatch (#527): actually drop GOVERNS edges for a decision doc "
            "that has vanished entirely. Default False keeps them across reindexes."
        ),
    )


class JobView(BaseModel):
    """One index job, as reported by the jobs store."""

    id: str
    slug: str
    state: str  # queued | running | done | failed | cancelled | interrupted
    phase: str | None
    progress: float  # 0..100
    symbol_count: int
    edge_count: int
    embedding_count: int
    error: str | None
    started_at: float
    updated_at: float
    finished_at: float | None
    governs_written: int = 0
    governs_dropped: int = 0
    governs_unresolved_spans: list[str] = Field(default_factory=list)
    governs_suspicious_docs: list[str] = Field(default_factory=list)
    entities_written: int = 0
    entities_dropped: int = 0
    entity_counts_by_kind: str = ""

    @classmethod
    def from_job(cls, job: CodeIndexJob) -> JobView:
        return cls(
            id=job.job_id,
            slug=job.slug,
            state=job.status,
            phase=job.phase,
            progress=job.progress_pct,
            symbol_count=job.symbol_count,
            edge_count=job.edge_count,
            embedding_count=job.embedding_count,
            error=job.error,
            started_at=job.started_at,
            updated_at=job.updated_at,
            finished_at=job.finished_at,
            governs_written=job.governs_written,
            governs_dropped=job.governs_dropped,
            governs_unresolved_spans=list(job.governs_unresolved_spans),
            governs_suspicious_docs=list(job.governs_suspicious_docs),
            entities_written=job.entities_written,
            entities_dropped=job.entities_dropped,
            entity_counts_by_kind=job.entity_counts_by_kind,
        )


class RepoView(BaseModel):
    """One indexed repo (registry row + last successful job's counts)."""

    slug: str
    repo_path: str
    last_indexed_at: int | None
    indexed_head: str | None
    """HEAD commit the index was built from."""
    current_head: str | None
    """Current HEAD of the working tree (for staleness comparison)."""
    is_stale: bool
    watch_enabled: bool
    symbol_count: int
    edge_count: int

    @classmethod
    def from_repo(
        cls,
        repo: IndexedRepo,
        *,
        last_done: CodeIndexJob | None,
        current_head: str | None = None,
    ) -> RepoView:
        return cls(
            slug=repo.slug,
            repo_path=repo.repo_path,
            last_indexed_at=repo.last_indexed_at,
            indexed_head=repo.head_sha,
            current_head=current_head,
            is_stale=(
                current_head is not None
                and repo.head_sha is not None
                and current_head != repo.head_sha
            ),
            watch_enabled=repo.watch_enabled,
            symbol_count=last_done.symbol_count if last_done else 0,
            edge_count=last_done.edge_count if last_done else 0,
        )


class WatchToggleRequest(BaseModel):
    """POST /code/repos/{slug}/watch body."""

    enabled: bool = Field(description="Enroll (true) or unenroll (false) this repo for watching.")


class WatchToggleView(BaseModel):
    """POST /code/repos/{slug}/watch response."""

    slug: str
    watch_enabled: bool
    watching: bool  # an observer is running right now (master switch on + started)
    master_switch: bool  # CODE_INDEX_WATCH in the running service


class MigrateLayoutRequest(BaseModel):
    """POST /code/migrate-layout body."""

    dry_run: bool = Field(
        default=False,
        description="Classify every registry row but change nothing.",
    )
    prune_missing: bool = Field(
        default=True,
        description=(
            "Drop registry rows whose repo_path has been gone long enough to be a "
            "deletion rather than a transient absence. Gated: the first sighting "
            "only starts the clock."
        ),
    )


class MigrateLayoutEntry(BaseModel):
    """One registry row's classification and disposition."""

    slug: str
    repo_path: str
    data_dir: str
    verdict: str  # current | legacy | missing | unreachable | busy
    action: str  # none | reindex | stamped | waiting | pruned | skipped
    job_id: str | None = None


class MigrateLayoutView(BaseModel):
    """POST /code/migrate-layout response.

    ``jobs`` are the force-index jobs enqueued for legacy-layout repos; a
    caller that wants a completed migration must poll them to a terminal state.
    """

    dry_run: bool
    total: int
    current: int
    legacy: int
    pruned: int
    unreachable: int = 0
    busy: int
    entries: list[MigrateLayoutEntry]
    jobs: list[JobView]


class PruneRequest(BaseModel):
    """POST /code/prune body."""

    slug: str | None = Field(
        default=None,
        description="Target row's slug. Omit to prune every ripe orphan (batch mode).",
    )
    repo_path: str | None = Field(
        default=None,
        description="Disambiguates a slug that has several checkouts; ignored in batch mode.",
    )
    dry_run: bool = Field(
        default=False,
        description="Classify and report dispositions but change nothing.",
    )
    force: bool = Field(
        default=False,
        description="Bypass the 7-day grace gate (explicit user intent to delete now).",
    )


class PruneEntry(BaseModel):
    """One registry row's prune disposition."""

    slug: str
    repo_path: str
    verdict: str  # pruned | stamped | waiting | live | unreachable | busy
    row_deleted: bool = False
    store_dir: str | None = None
    store_dir_removed: bool = False
    detail: str | None = None


class PruneView(BaseModel):
    """POST /code/prune response (single-target: total==1, one entry)."""

    dry_run: bool
    forced: bool
    total: int
    pruned: int
    stamped: int
    skipped: int
    entries: list[PruneEntry]


class CentralityEntry(BaseModel):
    qualified_name: str
    pagerank: float


class RepoStats(BaseModel):
    """GET /code/repos/{slug}/stats body."""

    slug: str
    counts_by_kind: dict[str, int]
    top_centrality: list[CentralityEntry]
    vector_count: int


class SymbolView(BaseModel):
    """One symbol-graph row (``/code/search/symbol``, ``/code/symbols/*``)."""

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

    @classmethod
    def from_symbol(cls, s: CodeSymbol) -> SymbolView:
        return cls(
            qualified_name=s.qualified_name,
            kind=s.kind,
            name=s.name,
            file_path=s.file_path,
            start_line=s.start_line,
            end_line=s.end_line,
            docstring=s.docstring,
            decorators=list(s.decorators),
            is_exported=s.is_exported,
            is_async=s.is_async,
            is_generator=s.is_generator,
            source_code=s.source_code,
        )


class CallSiteView(BaseModel):
    """One caller/callee hit (structural queries + ``/code/symbols/*``)."""

    qualified_name: str
    file_path: str | None
    line: int | None

    @classmethod
    def from_call_site(cls, s: CallSite) -> CallSiteView:
        return cls(qualified_name=s.qualified_name, file_path=s.file_path, line=s.line)


class DecisionView(BaseModel):
    """One decision governing the queried symbol (Knowledge module).

    Distinct from ``CallSiteView``: a decision is a markdown heading-chunk, so it
    carries a ``heading`` and body ``snippet`` and its ``start_line`` is a heading
    offset — not a call site.
    """

    qualified_name: str
    file_path: str | None
    start_line: int | None
    heading: str
    snippet: str | None

    @classmethod
    def from_decision(cls, d: DecisionRow) -> DecisionView:
        return cls(
            qualified_name=d.qualified_name,
            file_path=d.file_path,
            start_line=d.start_line,
            heading=d.heading,
            snippet=d.snippet,
        )


class CentralitySymbol(BaseModel):
    """One top-centrality row hydrated with its location."""

    qualified_name: str
    pagerank: float
    file_path: str | None
    start_line: int | None


class EntityEdgeView(BaseModel):
    """One typed entity edge (CONSTRAINTS, TOUCHES, REQUIRES, COMMAND, STAKEHOLDER)."""

    src: str
    dst: str
    kind: str
    file_path: str
    span: str | None = None

    @classmethod
    def from_edge(cls, e: CodeEdge) -> EntityEdgeView:
        return cls(
            src=e.src,
            dst=e.dst,
            kind=e.kind,
            file_path=e.file_path,
            span=e.span,
        )
