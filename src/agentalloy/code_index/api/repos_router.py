"""Indexed-repo endpoints (``/code/repos*``).

Rewrites the essence of codebase-indexer's ``routers/repos.py``: list the
registry, per-repo stats (kind counts + centrality top + vector count), and
reindex (a forced index job using the registry's stored repo_path).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from agentalloy.code_index.api.models import (
    CentralityEntry,
    JobView,
    MigrateLayoutEntry,
    MigrateLayoutRequest,
    MigrateLayoutView,
    RepoStats,
    RepoView,
    WatchToggleRequest,
    WatchToggleView,
)
from agentalloy.code_index.api.state import CodeIndexState, get_code_index_state
from agentalloy.code_index.ingest.watch import WatchCapacityError
from agentalloy.code_index.store import code_index_paths, open_code_index, remove_repo

router = APIRouter()


def _git_current_head(repo_path: Path) -> str | None:
    """Read HEAD commit sha from a git repo, or None on any failure."""
    try:
        import subprocess

        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    return None


@router.get("/repos", response_model=list[RepoView], summary="List indexed repos")
async def list_repos(
    state: CodeIndexState = Depends(get_code_index_state),
) -> list[RepoView]:
    out: list[RepoView] = []
    for repo in state.jobs.list_repos():
        done = state.jobs.list_jobs(slug=repo.slug, status={"done"}, limit=1)
        current_head = _git_current_head(Path(repo.repo_path))
        out.append(
            RepoView.from_repo(repo, last_done=done[0] if done else None, current_head=current_head)
        )
    return out


@router.get("/repos/{slug}/stats", response_model=RepoStats, summary="Per-repo graph/vector stats")
async def repo_stats(
    slug: str,
    state: CodeIndexState = Depends(get_code_index_state),
) -> RepoStats:
    repo = state.jobs.get_repo(slug)
    if repo is None:
        raise HTTPException(status_code=404, detail=f"no index for repo: {slug}")
    paths = code_index_paths(state.settings, slug, repo_path=repo.repo_path)
    if not paths.graph_path.exists():
        raise HTTPException(status_code=404, detail=f"no index for repo: {slug}")

    def _collect() -> RepoStats:
        # "service" role matches the job writer's connection config so DuckDB's
        # in-process instance cache shares the database with a running job.
        handles = open_code_index(state.settings, slug, role="service", repo_path=repo.repo_path)
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
    "/repos/{slug}/watch",
    response_model=WatchToggleView,
    summary="Enroll/unenroll a repo for file watching (reacts immediately)",
    responses={409: {"description": "The per-process watch capacity is exhausted"}},
)
async def set_watch(
    slug: str,
    req: WatchToggleRequest,
    state: CodeIndexState = Depends(get_code_index_state),
) -> WatchToggleView:
    repo = state.jobs.get_repo(slug)
    if repo is None:
        raise HTTPException(status_code=404, detail=f"no such repo: {slug}")
    master = state.watch is not None
    watching = False
    if req.enabled:
        # Start the observer BEFORE persisting enrollment so a capacity error
        # never leaves an enrolled-but-unwatchable row behind.
        repo_path = Path(repo.repo_path)
        if state.watch is not None and repo_path.is_dir():
            try:
                state.watch.start(slug, repo_path)
                watching = True
            except WatchCapacityError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
    elif state.watch is not None:
        state.watch.stop(slug)
    state.jobs.set_watch_enabled(slug, req.enabled, repo_path=repo.repo_path)
    return WatchToggleView(
        slug=slug, watch_enabled=req.enabled, watching=watching, master_switch=master
    )


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


@router.post(
    "/migrate-layout",
    response_model=MigrateLayoutView,
    summary="Migrate every registered repo to the per-checkout store layout",
)
async def migrate_layout(
    req: MigrateLayoutRequest,
    state: CodeIndexState = Depends(get_code_index_state),
) -> MigrateLayoutView:
    """Bring the whole registry onto the ``repos/{slug}/{path_key}/`` layout.

    Idempotent and unconditional by design: this is the automatic step
    ``agentalloy upgrade`` runs, so taking the update *is* the consent — there
    is no per-repo opt-in. Every registry row is classified:

    - ``current``  — ``data_dir`` already ends in this checkout's path key.
    - ``legacy``   — old ``repos/{slug}/`` layout; a forced reindex is enqueued,
      which writes the new location. The legacy directory is left in place
      (``code_index_paths`` still reads it as a fallback), so this is additive
      and safe to interrupt.
    - ``missing``  — ``repo_path`` is gone from disk; the row and its store are
      dropped when ``prune_missing`` is set. Without this the registry keeps
      accumulating dead rows and every future upgrade retries them forever.
    - ``busy``     — an index job is already active for the slug; left alone,
      the next run picks it up.

    Enqueues jobs but does not wait: poll ``jobs`` to a terminal state.
    """
    entries: list[MigrateLayoutEntry] = []
    jobs: list[JobView] = []
    counts = {"current": 0, "legacy": 0, "missing": 0, "busy": 0}

    for repo in state.jobs.list_repos():
        repo_path = Path(repo.repo_path)
        active = state.jobs.find_active(repo.slug)
        if active is not None:
            counts["busy"] += 1
            entries.append(
                MigrateLayoutEntry(
                    slug=repo.slug,
                    repo_path=repo.repo_path,
                    data_dir=repo.data_dir,
                    verdict="busy",
                    action="skipped",
                )
            )
            continue

        if not repo_path.is_dir():
            counts["missing"] += 1
            action = "none"
            if not req.dry_run and req.prune_missing:
                if state.watch is not None:
                    state.watch.stop(repo.slug)
                await asyncio.to_thread(remove_repo, state.settings, repo.slug)
                state.jobs.delete_repo(repo.slug)
                action = "pruned"
            entries.append(
                MigrateLayoutEntry(
                    slug=repo.slug,
                    repo_path=repo.repo_path,
                    data_dir=repo.data_dir,
                    verdict="missing",
                    action=action,
                )
            )
            continue

        expected = code_index_paths(state.settings, repo.slug, repo_path=repo.repo_path).repo_dir
        if Path(repo.data_dir) == expected:
            counts["current"] += 1
            entries.append(
                MigrateLayoutEntry(
                    slug=repo.slug,
                    repo_path=repo.repo_path,
                    data_dir=repo.data_dir,
                    verdict="current",
                    action="none",
                )
            )
            continue

        counts["legacy"] += 1
        job_id: str | None = None
        action = "none"
        if not req.dry_run:
            job = state.start_job(repo_path=repo_path, slug=repo.slug, force=True)
            jobs.append(JobView.from_job(job))
            job_id = job.job_id
            action = "reindex"
        entries.append(
            MigrateLayoutEntry(
                slug=repo.slug,
                repo_path=repo.repo_path,
                data_dir=repo.data_dir,
                verdict="legacy",
                action=action,
                job_id=job_id,
            )
        )

    return MigrateLayoutView(
        dry_run=req.dry_run,
        total=len(entries),
        current=counts["current"],
        legacy=counts["legacy"],
        pruned=sum(1 for e in entries if e.action == "pruned"),
        busy=counts["busy"],
        entries=entries,
        jobs=jobs,
    )
