"""Pydantic request/response models for the ``/code`` surface.

Import-light on purpose: this module must not pull in the tree-sitter engine
(only the jobs-store DTOs), so OpenAPI generation and tests can use the
models without the ``[code-index]`` extra's heavy imports.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from agentalloy.code_index.store import CodeIndexJob, IndexedRepo
from agentalloy.storage.protocols import CallSite, CodeSymbol


class IndexRequest(BaseModel):
    """POST /code/index body."""

    repo_path: str = Field(description="Absolute path to the repository to index.")
    force: bool = Field(default=False, description="Full rebuild: ignore stored content hashes.")
    index_markdown: bool = Field(default=True, description="Also chunk + embed markdown docs.")


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
        )


class RepoView(BaseModel):
    """One indexed repo (registry row + last successful job's counts)."""

    slug: str
    repo_path: str
    last_indexed_at: int | None
    head_sha: str | None
    symbol_count: int
    edge_count: int

    @classmethod
    def from_repo(cls, repo: IndexedRepo, *, last_done: CodeIndexJob | None) -> RepoView:
        return cls(
            slug=repo.slug,
            repo_path=repo.repo_path,
            last_indexed_at=repo.last_indexed_at,
            head_sha=repo.head_sha,
            symbol_count=last_done.symbol_count if last_done else 0,
            edge_count=last_done.edge_count if last_done else 0,
        )


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


class CentralitySymbol(BaseModel):
    """One top-centrality row hydrated with its location."""

    qualified_name: str
    pagerank: float
    file_path: str | None
    start_line: int | None
