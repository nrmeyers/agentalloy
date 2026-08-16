# pyright: reportPrivateUsage=false
"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentalloy.code_index.api.state import CodeIndexState

import httpx
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from agentalloy.api.anthropic_passthrough import AnthropicPassthroughClient
from agentalloy.api.compose_models import ErrorResponse
from agentalloy.api.compose_router import get_orchestrator
from agentalloy.api.compose_router import router as compose_router
from agentalloy.api.corpus_ingest_router import router as corpus_ingest_router
from agentalloy.api.diagnostics_router import DiagnosticsChecker
from agentalloy.api.diagnostics_router import router as diagnostics_router
from agentalloy.api.health_router import HealthChecker, ReadinessChecker
from agentalloy.api.health_router import router as health_router
from agentalloy.api.proxy_passthrough_router import router as passthrough_router
from agentalloy.api.proxy_responses_router import router as responses_router
from agentalloy.api.proxy_router import router as proxy_router
from agentalloy.api.retrieve_router import get_retrieve_orchestrator
from agentalloy.api.retrieve_router import router as retrieve_router
from agentalloy.api.skill_router import get_skill_store
from agentalloy.api.skill_router import router as skill_router
from agentalloy.api.state_router import (
    _repo_key_for,
    contract_router,
    get_state_store,
)
from agentalloy.api.state_router import router as state_router
from agentalloy.api.telemetry_router import TelemetryQuerier
from agentalloy.api.telemetry_router import router as telemetry_router
from agentalloy.config import configure_logging, get_settings
from agentalloy.embed_provider import EmbedClient, get_embed_client
from agentalloy.install import release_check
from agentalloy.orchestration.compose import (
    AssemblyStageError,
    ComposeOrchestrator,
    RetrievalStageError,
)
from agentalloy.orchestration.retrieve import RetrieveOrchestrator
from agentalloy.reads import InconsistentActiveVersionError
from agentalloy.runtime_state import RuntimeCache, load_runtime_cache
from agentalloy.storage.open import open_fragments, open_skills, open_telemetry
from agentalloy.storage.state_store import bind_process_store, open_state_store
from agentalloy.telemetry import DuckDBTelemetryWriter
from agentalloy.telemetry.phase_writer import PhaseTelemetryWriter
from agentalloy.web.config_api import router as web_config_router
from agentalloy.web.ops_api import router as web_ops_router
from agentalloy.web.skills_api import router as web_skills_router
from agentalloy.web.spa import mount_web_ui
from agentalloy.web.wizard_api import router as web_wizard_router

logger = logging.getLogger(__name__)


async def _release_check_loop() -> None:
    """Refresh the release-update cache on a slow cadence, off the request path.

    Runs ``release_check.refresh`` (a blocking urllib call) in a worker thread so
    the event loop never blocks, swallowing every error so a flaky network or
    disk can't take the service down. Propagates ``CancelledError`` to stop.
    """
    await asyncio.sleep(release_check.INITIAL_DELAY_SECONDS)
    while True:
        with suppress(Exception):
            await asyncio.to_thread(release_check.refresh)
        await asyncio.sleep(release_check.CHECK_INTERVAL_SECONDS)


async def _code_index_refresh_loop(state: CodeIndexState, interval: int) -> None:
    """Periodically self-heal drifted code-index repos, off the request path.

    Sleeps first so a briefly-lived app (TestClient / integration run) cancels the
    task before the first tick. ``refresh_stale_repos`` is loop-bound (it schedules
    incremental jobs via ``asyncio.create_task``) and swallows its own per-repo
    errors; the outer ``suppress`` guards the scan itself. Propagates
    ``CancelledError`` to stop.
    """
    while True:
        await asyncio.sleep(interval)
        with suppress(Exception):
            state.refresh_stale_repos()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the runtime store + embedding client for the app lifetime.

    Loads the active-skill cache at startup (NXS-777).  If loading fails the
    app still starts — ``app.state.runtime`` is ``None`` and the health
    endpoint reflects ``unavailable`` while runtime handlers 503.

    In tests we override ``get_orchestrator`` via ``app.dependency_overrides``
    so no real DuckDB/Lance or embedding connection is created.
    """
    settings = get_settings()
    settings.ensure_data_dirs()
    # Also ensure the container data dir exists (fixes Containerfile COPY issue).
    # Only create /app/data in deployment contexts (containers); native installs
    # that don't use /app should skip this silently.
    if Path("/.dockerenv").exists() or Path("/app").is_dir():
        try:
            Path("/app/data").mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # Read-only /app mount (or similar): best-effort — the dir may
            # already exist or be baked into the image. Don't crash startup.
            logger.warning("could not create /app/data: %s", exc)
    # Ensure the skill schema exists, then serve it READ-ONLY for the app's
    # lifetime. DuckDB grants a writer only while nothing else holds the file
    # — this read-only handle included — so out-of-process writers (the
    # reembed / install-pack CLIs) stop this service first (`agentalloy
    # reembed` does so automatically), and in-process writers (web reembed /
    # wizard install) wrap the write in ``store.released()``. The brief
    # writer-migrate here runs before serving begins. Fragments live in Lance
    # (MVCC, no lock); telemetry is a separate service-owned RW file.
    _writer = open_skills(settings, read_only=False)
    try:
        _writer.migrate()
    finally:
        _writer.close()
    store = open_skills(settings, read_only=True)
    vector_store = open_fragments(settings)
    telemetry_store = open_telemetry(settings, read_only=False)
    embed_client: EmbedClient = get_embed_client(settings)
    telemetry = DuckDBTelemetryWriter(telemetry_store)
    # Phase-event writer over the SAME telemetry.duck store (decision: no
    # second DuckDB handle). Lifespan-scoped so its schema DDL runs once per
    # process instead of on every proxy request — see proxy_router.py /
    # proxy_signal.py, which prefer this instance over constructing their own.
    phase_telemetry = PhaseTelemetryWriter(telemetry_store)

    # SDD state store — single-writer DuckDB, opened once for the process
    # lifetime.  Path sits alongside the corpus DuckDB file.
    # Rows are keyed by repo and every request re-scopes the handle
    # (``get_repo_store``); the deployment repo is only the default for callers
    # that send no ``repo_root``.  The re-key drains the pre-task-11 bucket,
    # where the key came from the DB filename and every repo shared one row.
    state_db_path = str(Path(settings.duckdb_path).parent / "state.duck")
    deployment_root = os.environ.get("AGENTALLOY_PROJECT_DIR") or str(Path.cwd())
    state_store = open_state_store(state_db_path, repo=_repo_key_for(deployment_root))
    try:
        state_store.rekey_legacy_rows(state_store.repo)
    except Exception:
        logger.warning("state store legacy re-key failed — continuing", exc_info=True)
    # Publish the handle to in-process callers that cannot take a `Depends`:
    # `signals.skill_loader` (proxy phase reads/writes) and the watcher. DuckDB
    # is single-writer, so they must share this one rather than open their own.
    bind_process_store(state_store)

    # AC-6: register the in-process store hook so the watcher fires post-commit
    # when the phase row changes.  One hook, not one per recorded repo: the
    # wiring records are read on each fire, so a repo wired against an already
    # running service is covered without a restart.  Harness-agnostic — the
    # registry knows only kinds and callables; per-harness output from
    # ``wire_harness`` is unchanged and stays.
    try:
        from agentalloy.watch.watcher import register_wired_repos_watcher  # noqa: PLC0415

        register_wired_repos_watcher(state_store)
    except Exception:
        logger.warning("watcher hook registration failed — continuing", exc_info=True)

    # --- NXS-777: startup-time cache load ---
    runtime: RuntimeCache | None = None
    runtime_load_error: str | None = None
    try:
        runtime = load_runtime_cache(store)
    except Exception as exc:
        logger.error("Runtime cache load failed — service will start in degraded mode: %s", exc)
        runtime_load_error = str(exc)

    app.state.runtime = runtime
    app.state.runtime_load_error = runtime_load_error

    # Wire orchestrators: prefer cache when available, fall back to store so
    # existing store-backed code paths still work (e.g. skill inspection).
    source = runtime if runtime is not None else store

    orchestrator = ComposeOrchestrator(
        source,
        embed_client,
        vector_store,
        telemetry,
        embedding_model=settings.runtime_embedding_model,
        settings=settings,
    )
    retrieve_orch = RetrieveOrchestrator(
        source,
        embed_client,
        vector_store,
        telemetry,
        embedding_model=settings.runtime_embedding_model,
    )
    app.dependency_overrides[get_orchestrator] = lambda: orchestrator
    app.dependency_overrides[get_retrieve_orchestrator] = lambda: retrieve_orch
    app.dependency_overrides[get_skill_store] = lambda: store  # inspection always live
    app.dependency_overrides[get_state_store] = lambda: state_store
    # Stashed so an in-process corpus write (web reembed / wizard install) can
    # rebind a freshly reloaded RuntimeCache — see web/runtime_refresh.py.
    app.state.compose_orchestrator = orchestrator
    app.state.retrieve_orchestrator = retrieve_orch
    health_checker = HealthChecker(
        store,
        embed_client,
        telemetry_store,
        settings.runtime_embedding_model,
        runtime_load_error=runtime_load_error,
        upstream_summary=(
            f"url={settings.upstream_url} model={settings.upstream_model}"
            if settings.upstream_configured()
            else None
        ),
    )
    app.state.health_checker = health_checker
    # Readiness checker reads bootstrap markers under /app. Wire it whenever
    # the directory exists; on native installs /app won't exist and the
    # endpoint falls back to "ready" via its None-checker default.
    app_dir = Path("/app")
    if app_dir.is_dir():
        app.state.readiness_checker = ReadinessChecker(app_dir=app_dir)
    app.state.diagnostics_checker = DiagnosticsChecker(store, runtime, health_checker)
    app.state.telemetry_querier = TelemetryQuerier(telemetry_store)
    # Expose for proxy router dependencies
    app.state.embed_client = embed_client
    # The Lance fragment store (vector + BM25). Name kept as ``vector_store`` for
    # the diagnostics/proxy app.state contract; it is a FragmentStore in v5.
    app.state.vector_store = vector_store
    # Service-owned telemetry.duck handle — the proxy trace writers and the
    # telemetry querier record/read composition traces here (decoupled from the
    # skill graph + Lance index so the reembed writer never contends — D4).
    app.state.telemetry_store = telemetry_store
    # Phase-event writer (task 04) — the proxy router / signal layer read this
    # to avoid constructing a fresh writer (and re-running its schema DDL) on
    # every single request. No separate close(): it holds no resource of its
    # own beyond telemetry_store, which is already closed below.
    app.state.phase_telemetry = phase_telemetry
    # Expose the live read-only SkillStore so diagnostics (e.g. corpus skill
    # counts) can reuse the open handle instead of opening another one.
    app.state.store = store

    # Async client for embed proxy passthrough
    import contextlib as _ctx

    embed_async_client: httpx.AsyncClient | None = None
    with _ctx.suppress(Exception):
        embed_async_client = httpx.AsyncClient(
            base_url=settings.runtime_embed_base_url.rstrip("/"),
            headers={"Content-Type": "application/json"},
            timeout=httpx.Timeout(connect=5.0, read=30.0),
        )
    app.state.embed_async_client = embed_async_client

    # Upstream LLM client (for proxy passthrough)
    upstream_client: httpx.AsyncClient | None = None
    if settings.upstream_configured():
        upstream_headers: dict[str, str] = {
            "Content-Type": "application/json",
        }
        if settings.upstream_api_key:
            upstream_headers["Authorization"] = f"Bearer {settings.upstream_api_key}"
        upstream_client = httpx.AsyncClient(
            base_url=settings.upstream_url.rstrip("/"),
            headers=upstream_headers,
            timeout=httpx.Timeout(connect=5.0, read=300.0, write=30.0, pool=5.0),
        )
    app.state.upstream_client = upstream_client

    # Per-repo upstream clients (adopted from a harness's own config via
    # `agentalloy add` → .agentalloy/upstream). Lazily populated per distinct
    # captured base_url by the proxy router; closed alongside the global client.
    app.state.upstream_client_cache = {}

    # Native Anthropic passthrough client (the /proj/<token>/v1/messages path).
    # Always constructed (default upstream https://api.anthropic.com). It holds
    # NO Anthropic credential — it forwards the caller's own, verbatim.
    anthropic_passthrough_client = AnthropicPassthroughClient(settings.anthropic_upstream_url)
    app.state.anthropic_passthrough_client = anthropic_passthrough_client

    # Native OpenAI Responses passthrough client (the /proj/<token>/v1/responses
    # path — codex et al.). Same auth-transparent contract; the client class is
    # protocol-agnostic despite its name. Spec: docs/responses-surface.md.
    responses_passthrough_client = AnthropicPassthroughClient(settings.responses_upstream_url)
    app.state.responses_passthrough_client = responses_passthrough_client

    # Code-index module state: constructed only when the module's router was
    # actually mounted (toggle on AND the [code-index] extra importable). All
    # code_index imports stay inside this branch so a disabled module never
    # imports tree-sitter. Jobs run as asyncio tasks tracked on the state.
    code_index_state = None
    _ci_provider = None
    if getattr(app.state, "module_status", {}).get("code_index") == "enabled":
        from agentalloy.code_index.api.state import CodeIndexState, get_code_index_state
        from agentalloy.code_index.store import open_jobs

        ci_jobs = open_jobs(settings)

        # (#526/#527) Store-presence check for the markdown-phase pruning
        # guard: a decision doc (``docs/design/<slug>/approach.md``) missing
        # on disk but still present as an SDD artifact row survives the
        # ingest pipeline's incremental prune. ``code_index/`` itself must
        # not import the state store, so the closure is built here (app.py
        # already has ``state_store`` in scope from its own construction
        # above) and injected as a per-repo-path factory.
        def _decision_source_exists_factory(
            repo_path: Path,
        ) -> Callable[[str], bool]:
            repo_key = _repo_key_for(str(repo_path))

            def _exists(doc_path: str) -> bool:
                m = re.match(r"^docs/design/([^/]+)/approach\.md$", doc_path)
                if m is None:
                    return False
                try:
                    scoped = state_store.for_repo(repo_key)
                    return scoped.get_artifact("design", m.group(1), "approach.md") is not None
                except Exception:  # noqa: BLE001 — best-effort guard, never break ingest
                    return False

            return _exists

        code_index_state = CodeIndexState(
            settings=settings,
            embed_client=embed_client,
            jobs=ci_jobs,
            decision_source_exists_factory=_decision_source_exists_factory,
        )
        # Wire the code-index state onto the orchestrator so the proxy
        # push path can reach it (settings is already wired).
        orchestrator.state = code_index_state
        # Retire active job rows orphaned by a previous (now-dead) process.
        ci_jobs.sweep_interrupted(code_index_state.worker_token)
        if settings.code_index_watch:
            # Master switch on: observers start for the ENROLLED registry
            # repos (per-repo `watch_enabled`), not for everything indexed.
            code_index_state.enable_watch(asyncio.get_running_loop())
            code_index_state.start_enrolled_watches()
        # Staleness nudge (log-only, no auto-reindex): one INFO line per repo
        # whose HEAD moved since its last index. Off the request path; the
        # method swallows git/registry failures so startup never breaks.
        code_index_state.log_stale_repos()
        # Auto-refresh (opt-in via CODE_INDEX_REFRESH_SECONDS>0): a periodic task
        # kicks an incremental reindex for repos whose HEAD drifted, so the index
        # self-heals between manual runs. Off by default; the container sets 300.
        if settings.code_index_refresh_seconds > 0:
            app.state.code_index_refresh_task = asyncio.create_task(
                _code_index_refresh_loop(code_index_state, settings.code_index_refresh_seconds),
            )
        _ci_provider = get_code_index_state
        app.dependency_overrides[_ci_provider] = lambda: code_index_state
        app.state.code_index_state = code_index_state

    # Background release-update check — the service's only outbound call, kept
    # off the request path. Throttled (once per CHECK_INTERVAL_SECONDS), fail-
    # silent, opt-out via AGENTALLOY_RELEASE_CHECK=0. The initial delay lets a
    # briefly-lived app (TestClient / integration run) cancel it before it ever
    # touches the network.
    app.state.release_check_task = asyncio.create_task(_release_check_loop())

    try:
        yield
    finally:
        task = getattr(app.state, "release_check_task", None)
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
        refresh_task = getattr(app.state, "code_index_refresh_task", None)
        if refresh_task is not None:
            refresh_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await refresh_task
        # Code-index shutdown: cancel + await running index tasks, stop
        # watches, close the jobs store (aclose does all three).
        if code_index_state is not None:
            with suppress(Exception):
                await code_index_state.aclose()
        if _ci_provider is not None:
            app.dependency_overrides.pop(_ci_provider, None)
        app.dependency_overrides.pop(get_orchestrator, None)
        app.dependency_overrides.pop(get_retrieve_orchestrator, None)
        app.dependency_overrides.pop(get_skill_store, None)
        app.dependency_overrides.pop(get_state_store, None)
        bind_process_store(None)
        # Guard each close independently: a failure in one (e.g. an in-flight
        # passthrough request at shutdown) must not skip the rest and leak the
        # DuckDB / Lance connections.
        cached_upstreams = list(getattr(app.state, "upstream_client_cache", {}).values())
        for aclient in (embed_async_client, upstream_client, *cached_upstreams):
            if aclient is not None:
                with suppress(Exception):
                    await aclient.aclose()
        with suppress(Exception):
            await anthropic_passthrough_client.aclose()
        with suppress(Exception):
            await responses_passthrough_client.aclose()
        # Per-repo passthrough clients (resolve_passthrough_client), cached on
        # app.state keyed by base_url when a request adopted a .agentalloy/upstream
        # override — same leak-guard pattern as cached_upstreams above.
        cached_passthrough = [
            *getattr(app.state, "anthropic_passthrough_client_cache", {}).values(),
            *getattr(app.state, "responses_passthrough_client_cache", {}).values(),
        ]
        for pclient in cached_passthrough:
            with suppress(Exception):
                await pclient.aclose()
        for closeable in (
            telemetry,
            embed_client,
            vector_store,
            store,
            telemetry_store,
            state_store,
        ):
            with suppress(Exception):
                closeable.close()


def _stage_error_response(stage: str, err: object) -> JSONResponse:
    assert isinstance(err, RetrievalStageError | AssemblyStageError)
    body = ErrorResponse(
        stage=stage,  # type: ignore[arg-type]
        code=err.code,
        message=err.message,
        available=err.available,
    )
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content=body.model_dump(),
    )


def create_app(*, use_default_lifespan: bool = True) -> FastAPI:
    """Build the FastAPI app.

    ``use_default_lifespan=False`` skips the production lifespan (which opens the
    DuckDB/Lance stores and the embedding client). Tests pass ``False`` and wire
    their own dependency overrides via ``app.dependency_overrides``.
    """
    configure_logging()
    settings = get_settings()
    app = FastAPI(
        title="agentalloy",
        version="1.0.0",
        description="Just-in-time context engine: instruction composition + code index.",
        lifespan=lifespan if use_default_lifespan else None,
    )

    @app.exception_handler(RetrievalStageError)
    async def _retrieval_handler(_req: Request, err: RetrievalStageError) -> JSONResponse:
        return _stage_error_response("retrieval", err)

    @app.exception_handler(AssemblyStageError)
    async def _assembly_handler(_req: Request, err: AssemblyStageError) -> JSONResponse:
        return _stage_error_response("assembly", err)

    @app.exception_handler(InconsistentActiveVersionError)
    async def _inconsistent_version_handler(
        _req: Request,
        err: InconsistentActiveVersionError,
    ) -> JSONResponse:
        body = {
            "code": "inconsistent_active_version",
            "skill_id": err.skill_id,
            "detail": str(err),
        }
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=body,
        )

    # Module registration. Health/diagnostics/telemetry and the web UI are
    # always mounted; each context module's routers register only when its
    # toggle is on, so a disabled module's endpoints 404 rather than 503.
    modules: dict[str, str] = {}

    app.include_router(health_router)
    app.include_router(diagnostics_router)
    app.include_router(telemetry_router)
    app.include_router(state_router)
    app.include_router(contract_router)

    if settings.compose_enabled:
        app.include_router(compose_router)
        app.include_router(retrieve_router)
        app.include_router(skill_router)
        app.include_router(corpus_ingest_router)
        app.include_router(proxy_router)
        app.include_router(passthrough_router)
        app.include_router(responses_router)
        modules["compose"] = "enabled"
    else:
        modules["compose"] = "disabled"

    if settings.code_index_enabled:
        # Lazy import: the module lives behind the [code-index] extra, and a
        # disabled (or uninstalled) module must never import tree-sitter.
        try:
            from agentalloy.code_index.api import build_code_index_router

            app.include_router(build_code_index_router())
            modules["code_index"] = "enabled"
        except ImportError as exc:
            logger.error(
                "CODE_INDEX_ENABLED is set but the code-index module is not "
                "installed — starting without it. Install the code-index "
                "dependencies (`uv sync`) and restart the service. (%s)",
                exc,
            )
            modules["code_index"] = "unavailable"
    else:
        modules["code_index"] = "disabled"

    app.state.module_status = modules

    app.include_router(web_config_router)
    app.include_router(web_skills_router)
    app.include_router(web_ops_router)
    app.include_router(web_wizard_router)
    # Mount LAST: the SPA's catch-all static mount must lose to every API route.
    mount_web_ui(app)

    return app


app = create_app()
