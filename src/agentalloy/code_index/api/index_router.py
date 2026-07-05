"""Index-job endpoints (``/code/index*``).

Rewrites the essence of codebase-indexer's ``routers/index.py`` job surface:
202 + job snapshot on start, 409 on a duplicate active job per slug,
status/list/cancel, and repo removal (refused while a job is active).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query

from agentalloy.code_index.api.models import IndexRequest, JobView
from agentalloy.code_index.api.state import CodeIndexState, get_code_index_state
from agentalloy.code_index.slug import repo_slug
from agentalloy.code_index.store import remove_repo

router = APIRouter()


@router.post(
    "/index",
    status_code=202,
    response_model=JobView,
    summary="Start an index job for a repository",
    responses={409: {"description": "An index job for this repo is already active"}},
)
async def start_index(
    req: IndexRequest,
    state: CodeIndexState = Depends(get_code_index_state),
) -> JobView:
    repo_path = Path(req.repo_path).expanduser()
    if not repo_path.is_dir():
        raise HTTPException(status_code=400, detail=f"repo_path is not a directory: {repo_path}")
    # repo_slug probes git remotes (subprocess) — keep it off the event loop.
    slug = await asyncio.to_thread(repo_slug, repo_path)
    active = state.jobs.find_active(slug)
    if active is not None:
        raise HTTPException(
            status_code=409,
            detail=f"an index job for slug {slug!r} is already active: {active.job_id}",
        )
    job = state.start_job(
        repo_path=repo_path, slug=slug, force=req.force, index_markdown=req.index_markdown
    )
    return JobView.from_job(job)


@router.get("/index/jobs", response_model=list[JobView], summary="List index jobs (newest first)")
async def list_jobs(
    slug: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    state: CodeIndexState = Depends(get_code_index_state),
) -> list[JobView]:
    return [JobView.from_job(j) for j in state.jobs.list_jobs(slug=slug, limit=limit)]


@router.get("/index/{job_id}/status", response_model=JobView, summary="One job's status")
async def job_status(
    job_id: str,
    state: CodeIndexState = Depends(get_code_index_state),
) -> JobView:
    job = state.jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"no such job: {job_id}")
    return JobView.from_job(job)


@router.post(
    "/index/{job_id}/cancel",
    response_model=JobView,
    summary="Request cancellation of an active job",
    responses={409: {"description": "Job is already terminal"}},
)
async def cancel_job(
    job_id: str,
    state: CodeIndexState = Depends(get_code_index_state),
) -> JobView:
    job = state.jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"no such job: {job_id}")
    if not state.jobs.request_cancel(job_id):
        raise HTTPException(status_code=409, detail=f"job {job_id} is not active ({job.status})")
    refreshed = state.jobs.get_job(job_id)
    assert refreshed is not None  # row existed above; cancel never deletes
    return JobView.from_job(refreshed)


@router.delete(
    "/index/{repo_slug}",
    summary="Remove an indexed repo (store directory + registry row)",
    responses={409: {"description": "An index job for this repo is active"}},
)
async def delete_repo(
    repo_slug: str,
    state: CodeIndexState = Depends(get_code_index_state),
) -> dict[str, object]:
    active = state.jobs.find_active(repo_slug)
    if active is not None:
        raise HTTPException(
            status_code=409,
            detail=f"cannot remove {repo_slug!r} while job {active.job_id} is active",
        )
    if state.watch is not None:
        state.watch.stop(repo_slug)
    removed_store = await asyncio.to_thread(remove_repo, state.settings, repo_slug)
    removed_registry = state.jobs.delete_repo(repo_slug)
    if not removed_store and not removed_registry:
        raise HTTPException(status_code=404, detail=f"no such repo: {repo_slug}")
    return {"slug": repo_slug, "removed": True}
