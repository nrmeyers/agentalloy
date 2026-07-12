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
        # Successful index of a watch-ENROLLED repo: begin watching it. A repo
        # that never opted in (`agentalloy code watch enable`) is left alone.
        if result.status == "done" and self.watch is not None:
            repo = self.jobs.get_repo(slug)
            if repo is not None and repo.watch_enabled:
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

    def start_enrolled_watches(self) -> list[str]:
        """Start observers for every watch-enrolled registry repo (startup).

        No-op when the master switch is off (:meth:`enable_watch` not called).
        Best-effort per repo: a missing path or a capacity/observer failure
        skips that repo (logged) and never breaks startup. Returns the slugs
        actually started.
        """
        if self.watch is None:
            return []
        started: list[str] = []
        for repo in self.jobs.list_watch_enabled_repos():
            repo_path = Path(repo.repo_path)
            if not repo_path.is_dir():
                logger.warning(
                    "code_index.watch skipping enrolled repo %s: path missing (%s)",
                    repo.slug,
                    repo_path,
                )
                continue
            try:
                self.watch.start(repo.slug, repo_path)
                started.append(repo.slug)
            except Exception:  # noqa: BLE001 — startup must survive one bad repo
                logger.warning("code_index.watch failed to start slug=%s", repo.slug, exc_info=True)
        return started

    def log_stale_repos(self) -> None:
        """One INFO line per registry repo whose HEAD moved since its index.

        Startup-only nudge (no auto-reindex). Fully wrapped: git failures,
        missing paths, and non-git dirs are silent.
        """
        from agentalloy.code_index.staleness import check_staleness

        try:
            repos = self.jobs.list_repos()
        except Exception:  # noqa: BLE001 — never break startup over a nudge
            logger.debug("code_index staleness check skipped", exc_info=True)
            return
        for repo in repos:
            try:
                verdict = check_staleness(Path(repo.repo_path), repo.head_sha)
            except Exception:  # noqa: BLE001
                continue
            if not verdict.stale:
                continue
            behind = (
                f" ({verdict.commits_behind} commits behind)"
                if verdict.commits_behind is not None
                else ""
            )
            logger.info(
                "code_index: repo %s is stale%s — reindex with `agentalloy code index %s`",
                repo.slug,
                behind,
                repo.repo_path,
            )

    def refresh_stale_repos(self) -> list[str]:
        """Kick an INCREMENTAL reindex for every registry repo whose HEAD moved.

        The staleness-driven analogue of :meth:`log_stale_repos`: instead of only
        logging, it starts a non-force (incremental — per-symbol content-hash diff)
        job so a drifted index self-heals without a manual ``agentalloy code index``.

        Reuses the repo's REGISTRY slug (``repo.slug``), never re-deriving it: a
        git-remote-vs-path slug drift would index into a fresh slug and orphan the
        existing store (the git-slug-churn trap). Skips repos with an active job
        (no double-run) and missing paths. Best-effort per repo — a git or pipeline
        failure skips that repo and never breaks the loop. Returns the kicked slugs.
        """
        from agentalloy.code_index.staleness import check_staleness

        try:
            repos = self.jobs.list_repos()
        except Exception:  # noqa: BLE001 — a registry read failure must not kill the loop
            logger.debug("code_index auto-refresh skipped: registry read failed", exc_info=True)
            return []
        kicked: list[str] = []
        for repo in repos:
            try:
                repo_path = Path(repo.repo_path)
                if not repo_path.is_dir():
                    continue
                if not check_staleness(repo_path, repo.head_sha).stale:
                    continue
                if self.jobs.find_active(repo.slug) is not None:
                    continue  # a job (manual, watch, or a prior tick) is already running
                self.start_job(repo_path=repo_path, slug=repo.slug, force=False)
                kicked.append(repo.slug)
            except Exception:  # noqa: BLE001 — one bad repo must not stop the rest
                logger.debug("code_index auto-refresh skipped slug=%s", repo.slug, exc_info=True)
                continue
        if kicked:
            logger.info("code_index auto-refresh: kicked incremental reindex for %s", kicked)
        return kicked

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
