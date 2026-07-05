"""Shared request helpers for the ``/code`` read routers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from fastapi import HTTPException

from agentalloy.code_index.api.state import CodeIndexState
from agentalloy.code_index.store import IndexedRepo, open_code_index
from agentalloy.storage.protocols import CodeIndexHandles


def require_indexed_repo(state: CodeIndexState, slug: str) -> IndexedRepo:
    """404 unless ``slug`` is in the indexed_repos registry."""
    repo = state.jobs.get_repo(slug)
    if repo is None:
        raise HTTPException(
            status_code=404,
            detail=f"repo {slug!r} is not indexed; index it via POST /code/index first",
        )
    return repo


async def with_handles[T](
    state: CodeIndexState, slug: str, fn: Callable[[CodeIndexHandles], T]
) -> T:
    """Run a synchronous store read in a worker thread with open/close managed.

    ``service`` role matches the job writer's connection config so DuckDB's
    in-process instance cache shares the database with a running job.
    """

    def _run() -> T:
        handles = open_code_index(state.settings, slug, role="service")
        try:
            return fn(handles)
        finally:
            handles.close()

    return await asyncio.to_thread(_run)
