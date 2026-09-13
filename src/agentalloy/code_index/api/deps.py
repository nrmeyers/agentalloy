"""Shared request helpers for the ``/code`` read routers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from fastapi import HTTPException

from agentalloy.code_index.api.state import CodeIndexState
from agentalloy.code_index.store import IndexedRepo, open_code_index
from agentalloy.storage.protocols import CodeIndexHandles


def require_indexed_repo(
    state: CodeIndexState,
    slug: str,
    *,
    repo_path: str | None = None,
) -> IndexedRepo:
    """404 unless ``slug`` is in the indexed_repos registry.

    With ``repo_path`` provided, resolves the exact (slug, repo_path) entry.
    Without it, falls back to the sole entry for the slug (returns 404 when
    ambiguous — multiple checkouts of the same remote).
    """
    if repo_path is not None:
        repo = state.jobs.get_repo(slug, repo_path=repo_path)
    else:
        repo = state.jobs.get_repo(slug)
    if repo is None:
        raise HTTPException(
            status_code=404,
            detail=f"repo {slug!r} is not indexed; index it via POST /code/index first",
        )
    return repo


async def with_handles[T](
    state: CodeIndexState,
    slug: str,
    fn: Callable[[CodeIndexHandles], T],
    *,
    repo_path: str | None = None,
) -> T:
    """Run a synchronous store read in a worker thread with open/close managed.

    ``service`` role opens read-only so API reads never contend with a
    running index job's writes.

    When ``repo_path`` is provided, the data directory is scoped to that
    specific checkout (multiple checkouts of the same remote coexist).
    """

    def _run() -> T:
        handles = open_code_index(state.settings, slug, role="service", repo_path=repo_path)
        try:
            return fn(handles)
        finally:
            handles.close()

    return await asyncio.to_thread(_run)
