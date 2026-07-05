"""Indexed-repo endpoints (``/code/repos*``).

Rewrites the essence of codebase-indexer's ``routers/repos.py``: list the
registry, per-repo stats (kind counts + centrality top + vector count), and
reindex (a forced index job using the registry's stored repo_path).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from agentalloy.code_index.api.models import CentralityEntry, JobView, RepoStats, RepoView
from agentalloy.code_index.api.state import CodeIndexState, get_code_index_state
from agentalloy.code_index.store import code_index_paths, open_code_index

router = APIRouter()


@router.get("/repos", response_model=list[RepoView], summary="List indexed repos")
async def list_repos(
    state: CodeIndexState = Depends(get_code_index_state),
) -> list[RepoView]:
    out: list[RepoView] = []
    for repo in state.jobs.list_repos():
        done = state.jobs.list_jobs(slug=repo.slug, status={"done"}, limit=1)
        out.append(RepoView.from_repo(repo, last_done=done[0] if done else None))
    return out


@router.get("/repos/{slug}/stats", response_model=RepoStats, summary="Per-repo graph/vector stats")
async def repo_stats(
    slug: str,
    state: CodeIndexState = Depends(get_code_index_state),
) -> RepoStats:
    if not code_index_paths(state.settings, slug).graph_path.exists():
        raise HTTPException(status_code=404, detail=f"no index for repo: {slug}")

    def _collect() -> RepoStats:
        # "service" role matches the job writer's connection config so DuckDB's
        # in-process instance cache shares the database with a running job.
        handles = open_code_index(state.settings, slug, role="service")
        try:
            return RepoStats(
                slug=slug,
                counts_by_kind=handles.graph.counts_by_kind(),
                top_centrality=[
                    CentralityEntry(qualified_name=qn, pagerank=score)
                    for qn, score in handles.graph.top_centrality(10)
                ],
                vector_count=handles.vectors.count(),
            )
        finally:
            handles.close()

    return await asyncio.to_thread(_collect)


@router.post(
    "/repos/{slug}/reindex",
    status_code=202,
    response_model=JobView,
    summary="Force a full reindex using the registry's stored repo path",
    responses={409: {"description": "An index job for this repo is already active"}},
)
async def reindex_repo(
    slug: str,
    state: CodeIndexState = Depends(get_code_index_state),
) -> JobView:
    repo = state.jobs.get_repo(slug)
    if repo is None:
        raise HTTPException(status_code=404, detail=f"no such repo: {slug}")
    repo_path = Path(repo.repo_path)
    if not repo_path.is_dir():
        raise HTTPException(
            status_code=400, detail=f"stored repo_path no longer exists: {repo_path}"
        )
    active = state.jobs.find_active(slug)
    if active is not None:
        raise HTTPException(
            status_code=409,
            detail=f"an index job for slug {slug!r} is already active: {active.job_id}",
        )
    job = state.start_job(repo_path=repo_path, slug=slug, force=True)
    return JobView.from_job(job)
