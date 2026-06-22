"""FastAPI application factory."""

from __future__ import annotations

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
from agentalloy.api.proxy_anthropic_router import router as anthropic_router
from agentalloy.api.proxy_passthrough_router import router as passthrough_router
from agentalloy.api.proxy_router import router as proxy_router
from agentalloy.api.retrieve_router import get_retrieve_orchestrator
from agentalloy.api.retrieve_router import router as retrieve_router
from agentalloy.api.skill_router import get_skill_store
from agentalloy.api.skill_router import router as skill_router
from agentalloy.api.telemetry_router import TelemetryQuerier
from agentalloy.api.telemetry_router import router as telemetry_router
from agentalloy.config import get_settings
from agentalloy.embed_provider import EmbedClient, get_embed_client
from agentalloy.orchestration.compose import (
    AssemblyStageError,
    ComposeOrchestrator,
    RetrievalStageError,
)
from agentalloy.orchestration.retrieve import RetrieveOrchestrator
from agentalloy.reads import InconsistentActiveVersion
from agentalloy.runtime_state import RuntimeCache, load_runtime_cache
from agentalloy.storage.ladybug import LadybugStore
from agentalloy.storage.vector_store import VectorStore, open_or_create
from agentalloy.telemetry import DuckDBTelemetryWriter

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the runtime store + Ollama client for the app lifetime.

    Loads the active-skill cache at startup (NXS-777).  If loading fails the
    app still starts — ``app.state.runtime`` is ``None`` and the health
    endpoint reflects ``unavailable`` while runtime handlers 503.

    In tests we override ``get_orchestrator`` via ``app.dependency_overrides``
    so no real LadybugDB or Ollama connection is created.
    """
    settings = get_settings()
    settings.ensure_data_dirs()
    # Also ensure the container data dir exists (fixes Containerfile COPY issue).
    # Only create /app/data in deployment contexts (containers); native installs
    # that don't use /app should skip this silently.
    if Path("/.dockerenv").exists() or Path("/app").is_dir():
        Path("/app/data").mkdir(parents=True, exist_ok=True)
    store = LadybugStore(settings.ladybug_db_path)
    store.open()
    vector_store: VectorStore = open_or_create(settings.duckdb_path)
    embed_client: EmbedClient = get_embed_client(settings)
    telemetry = DuckDBTelemetryWriter(vector_store)

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
    health_checker = HealthChecker(
        store,
        embed_client,
        vector_store,
        settings.runtime_embedding_model,
        runtime_load_error=runtime_load_error,
    )
    app.state.health_checker = health_checker
    # Readiness checker reads bootstrap markers under /app. Wire it whenever
    # the directory exists; on native installs /app won't exist and the
    # endpoint falls back to "ready" via its None-checker default.
    app_dir = Path("/app")
    if app_dir.is_dir():
        app.state.readiness_checker = ReadinessChecker(app_dir=app_dir)
    app.state.diagnostics_checker = DiagnosticsChecker(store, runtime, health_checker)
    app.state.telemetry_querier = TelemetryQuerier(vector_store)
    # Expose for proxy router dependencies
    app.state.embed_client = embed_client
    app.state.vector_store = vector_store

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

    # Native Anthropic passthrough client (the /proj/<token>/v1/messages path).
    # Always constructed (default upstream https://api.anthropic.com). It holds
    # NO Anthropic credential — it forwards the caller's own, verbatim.
    anthropic_passthrough_client = AnthropicPassthroughClient(settings.anthropic_upstream_url)
    app.state.anthropic_passthrough_client = anthropic_passthrough_client

    try:
        yield
    finally:
        app.dependency_overrides.pop(get_orchestrator, None)
        app.dependency_overrides.pop(get_retrieve_orchestrator, None)
        app.dependency_overrides.pop(get_skill_store, None)
        # Guard each close independently: a failure in one (e.g. an in-flight
        # passthrough request at shutdown) must not skip the rest and leak the
        # DuckDB / LadybugDB connections.
        for aclient in (embed_async_client, upstream_client):
            if aclient is not None:
                with suppress(Exception):
                    await aclient.aclose()
        with suppress(Exception):
            await anthropic_passthrough_client.aclose()
        for closeable in (telemetry, embed_client, vector_store, store):
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

    ``use_default_lifespan=False`` skips the production lifespan (which opens
    LadybugDB and the Ollama client). Tests pass ``False`` and wire their own
    dependency overrides via ``app.dependency_overrides``.
    """
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

    app.include_router(health_router)
    app.include_router(compose_router)
    app.include_router(retrieve_router)
    app.include_router(skill_router)
    app.include_router(diagnostics_router)
    app.include_router(telemetry_router)
    app.include_router(proxy_router)
    app.include_router(anthropic_router)
    app.include_router(passthrough_router)

    return app


app = create_app()
