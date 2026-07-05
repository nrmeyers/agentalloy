"""Module state + DI provider for the ``/code`` routers.

Follows the agentalloy DI idiom (see ``api.compose_router.get_orchestrator``):
:func:`get_code_index_state` raises by default and the app lifespan binds the
real :class:`CodeIndexState` via ``app.dependency_overrides``. Tests bind a
state built around a fake embed client the same way.

Jobs run as asyncio tasks INSIDE the service process (the service is the
code-index writer); the task registry lives here so shutdown can cancel and
await them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from agentalloy.code_index.ingest.pipeline import run_index_job
from agentalloy.code_index.ingest.watch import WatchManager
from agentalloy.code_index.store import CodeIndexJob, CodeIndexJobsStore
from agentalloy.config import Settings
from agentalloy.embed_provider import EmbedClient

logger = logging.getLogger(__name__)


@dataclass
class CodeIndexState:
    """Everything the ``/code`` routers need, stashed on ``app.state``."""

    settings: Settings
    embed_client: EmbedClient
    jobs: CodeIndexJobsStore
    worker_token: str = field(default_factory=lambda: uuid.uuid4().hex)
    tasks: dict[str, asyncio.Task[None]] = field(
        default_factory=lambda: dict[str, asyncio.Task[None]]()
    )
    watch: WatchManager | None = None

    def start_job(
        self, *, repo_path: Path, slug: str, force: bool, index_markdown: bool = True
    ) -> CodeIndexJob:
        """Create the job row and schedule the pipeline as a background task.

        Callers are responsible for the duplicate-slug (409) check; the row is
        created before the task so the response snapshot always exists.
        """
        job = self.jobs.create_job(
            slug=slug,
            repo_path=str(repo_path),
            force_reindex=force,
            worker_token=self.worker_token,
        )
        task = asyncio.create_task(
            self._run(
                job.job_id,
                repo_path=repo_path,
                slug=slug,
                force=force,
                index_markdown=index_markdown,
            ),
            name=f"code-index:{slug}:{job.job_id}",
        )
        self.tasks[job.job_id] = task
        task.add_done_callback(lambda _t, jid=job.job_id: self.tasks.pop(jid, None))
        return job

    async def _run(
        self, job_id: str, *, repo_path: Path, slug: str, force: bool, index_markdown: bool
    ) -> None:
        result = await run_index_job(
            self.settings,
            self.embed_client,
            self.jobs,
            repo_path=repo_path,
            slug=slug,
            force=force,
            index_markdown=index_markdown,
            job_id=job_id,
        )
        # Successful index of a watched-eligible repo: begin watching it.
        if result.status == "done" and self.watch is not None:
            with contextlib.suppress(Exception):
                self.watch.start(slug, repo_path)

    def enable_watch(self, loop: asyncio.AbstractEventLoop) -> None:
        """Construct the watch manager (``CODE_INDEX_WATCH=1`` only).

        The watchdog callback fires on an observer thread; it is marshalled
        onto ``loop`` where a fresh incremental job starts unless one for the
        slug is already active.
        """

        def _kick(slug: str, repo_path: Path) -> None:
            if self.jobs.find_active(slug) is None:
                self.start_job(repo_path=repo_path, slug=slug, force=False)

        def _on_change(slug: str, repo_path: Path) -> None:
            loop.call_soon_threadsafe(_kick, slug, repo_path)

        self.watch = WatchManager(_on_change)

    async def aclose(self) -> None:
        """Stop watches, cancel + await running index tasks, close the store."""
        if self.watch is not None:
            with contextlib.suppress(Exception):
                self.watch.stop_all()
        pending = list(self.tasks.values())
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self.tasks.clear()
        self.jobs.close()


def get_code_index_state() -> CodeIndexState:
    """DI provider — overridden during the app lifespan (or by tests)."""
    raise RuntimeError(
        "get_code_index_state must be bound during app lifespan; no default available"
    )
