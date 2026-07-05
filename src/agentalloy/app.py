"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from agentalloy.api.anthropic_passthrough import AnthropicPassthroughClient
from agentalloy.api.compose_models import ErrorResponse
from agentalloy.api.compose_router import get_orchestrator
from agentalloy.api.compose_router import router as compose_router
from agentalloy.api.diagnostics_router import DiagnosticsChecker
from agentalloy.api.diagnostics_router import router as diagnostics_router
from agentalloy.api.health_router import HealthChecker, ReadinessChecker
from agentalloy.api.health_router import router as health_router
from agentalloy.api.proxy_passthrough_router import router as passthrough_router
from agentalloy.api.proxy_router import router as proxy_router
from agentalloy.api.retrieve_router import get_retrieve_orchestrator
from agentalloy.api.retrieve_router import router as retrieve_router
from agentalloy.api.skill_router import get_skill_store
from agentalloy.api.skill_router import router as skill_router
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
from agentalloy.reads import InconsistentActiveVersion
from agentalloy.runtime_state import RuntimeCache, load_runtime_cache
from agentalloy.storage.open import open_fragments, open_skills, open_telemetry
from agentalloy.telemetry import DuckDBTelemetryWriter
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
        Path("/app/data").mkdir(parents=True, exist_ok=True)
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
        app.dependency_overrides.pop(get_orchestrator, None)
        app.dependency_overrides.pop(get_retrieve_orchestrator, None)
        app.dependency_overrides.pop(get_skill_store, None)
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
        for closeable in (telemetry, embed_client, vector_store, store, telemetry_store):
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
        description="Runtime skill composition service.",
        lifespan=lifespan if use_default_lifespan else None,
    )

    @app.exception_handler(RetrievalStageError)
    async def _retrieval_handler(_req: Request, err: RetrievalStageError) -> JSONResponse:
        return _stage_error_response("retrieval", err)

    @app.exception_handler(AssemblyStageError)
    async def _assembly_handler(_req: Request, err: AssemblyStageError) -> JSONResponse:
        return _stage_error_response("assembly", err)

    @app.exception_handler(InconsistentActiveVersion)
    async def _inconsistent_version_handler(
        _req: Request, err: InconsistentActiveVersion
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

    if settings.compose_enabled:
        app.include_router(compose_router)
        app.include_router(retrieve_router)
        app.include_router(skill_router)
        app.include_router(proxy_router)
        app.include_router(passthrough_router)
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
                "installed — starting without it. Install with: "
                "uv tool install 'agentalloy[code-index]' (%s)",
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
