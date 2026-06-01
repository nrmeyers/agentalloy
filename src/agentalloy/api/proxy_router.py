"""Proxy router — forwards chat completions to the upstream LLM.

Full integrated handler:
  parse -> resolve cwd -> signal layer -> compose+inject -> forward -> telemetry

Handles both non-streaming (JSON) and streaming (SSE) responses.
Composition failures soft-fail: request passes through unchanged.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from agentalloy.api.proxy_context import read_phase, resolve_working_dir
from agentalloy.api.proxy_injection import compose_and_inject
from agentalloy.api.proxy_models import ProxyRequest
from agentalloy.api.proxy_signal import evaluate_signal
from agentalloy.api.proxy_telemetry import write_proxy_trace

if TYPE_CHECKING:
    from agentalloy.config import Settings as AppSettings
    from agentalloy.embed_provider import EmbedClient
    from agentalloy.orchestration.compose import ComposeOrchestrator
    from agentalloy.storage.vector_store import VectorStore

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Dependency providers — overridden in tests via app.dependency_overrides[]
# ---------------------------------------------------------------------------


def get_upstream_client(request: Request) -> httpx.AsyncClient | None:
    """Return the upstream LLM httpx.AsyncClient (lifespan-scoped, via app.state).

    Returns None if the upstream is not configured.
    """
    return getattr(request.app.state, "upstream_client", None)


def get_embed_client(request: Request) -> EmbedClient | None:
    """Return the embedding client from app.state."""
    return getattr(request.app.state, "embed_client", None)


def get_embed_async_client(request: Request) -> httpx.AsyncClient | None:
    """Return the async embed client from app.state for proxy passthrough."""
    return getattr(request.app.state, "embed_async_client", None)


def get_vector_store(request: Request) -> VectorStore | None:
    """Return the VectorStore from app.state."""
    return getattr(request.app.state, "vector_store", None)


def get_orchestrator_for_proxy(request: Request) -> ComposeOrchestrator | None:
    """Return the ComposeOrchestrator via dependency overrides or app.state."""
    # Try the dependency override pattern (same as compose_router)
    try:
        from agentalloy.api.compose_router import get_orchestrator

        app = request.app
        override = app.dependency_overrides.get(get_orchestrator)
        if override is not None:
            return override()
    except Exception:  # noqa: BLE001
        pass
    return None


def get_settings_for_proxy(request: Request) -> AppSettings:
    """Return Settings instance for proxy (used for upstream_model override)."""
    from agentalloy.config import Settings as AppSettings

    return AppSettings()


# ---------------------------------------------------------------------------
# Error responses
# ---------------------------------------------------------------------------


def _upstream_not_configured_error() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "error": {
                "code": "upstream_not_configured",
                "message": "Upstream LLM is not configured. Set UPSTREAM_URL and UPSTREAM_MODEL.",
            }
        },
    )


def _upstream_unavailable_error(detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "error": {
                "code": "upstream_unavailable",
                "message": f"Upstream LLM unavailable: {detail}",
            }
        },
    )


# ---------------------------------------------------------------------------
# Streaming helper
# ---------------------------------------------------------------------------


def _stream_upstream_response(
    upstream: httpx.AsyncClient, payload: dict[str, Any]
) -> StreamingResponse:
    """Forward a streaming (SSE) response from the upstream LLM."""

    async def event_generator() -> AsyncGenerator[str, None]:
        async with upstream.stream("POST", "/v1/chat/completions", json=payload) as resp:
            if resp.status_code >= 500:
                logger.warning("Upstream streaming returned HTTP %d", resp.status_code)
                yield f'data: {{"error": "Upstream returned HTTP {resp.status_code}"}}\n\n'
                return
            async for chunk in resp.aiter_text():
                yield chunk

    return StreamingResponse(
        content=event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Request payload builder
# ---------------------------------------------------------------------------


def _resolve_model(model: str, upstream_model: str | None) -> str | None:
    """Resolve a model name to the upstream model to forward.

    The synthetic name ``"agentalloy-proxy"`` (used by Continue and other
    harnesses that point their API base at the proxy) maps to
    ``upstream_model`` from settings.  If upstream_model is unset, returns
    ``None`` so the caller can return a 503 with a clear message.

    Any other name is passed through unchanged, which allows callers that
    already specify a concrete model (e.g. ``"gpt-4o"``) to work without
    re-configuration.
    """
    if model == "agentalloy-proxy":
        return upstream_model if upstream_model else None
    return model


def _build_payload(request: ProxyRequest, upstream_model: str | None = None) -> dict[str, Any]:
    """Build the JSON payload to forward to the upstream LLM.

    If *upstream_model* is set, overrides ``request.model`` so that synthetic
    model names (e.g. "agentalloy-proxy" from Continue) are mapped to the
    actual upstream model.

    Raises ``ValueError`` if the resolved model is ``None`` (i.e., the
    client sent ``"agentalloy-proxy"`` but no upstream model is configured).
    """
    resolved = _resolve_model(request.model, upstream_model)
    if resolved is None:
        raise ValueError(
            "Model 'agentalloy-proxy' requires an upstream model. "
            "Set UPSTREAM_MODEL in your configuration."
        )
    payload: dict[str, Any] = {
        "model": resolved,
        "messages": [m.model_dump() for m in request.messages],
        "stream": request.stream,
    }
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    if request.max_tokens is not None:
        payload["max_tokens"] = request.max_tokens
    if request.top_p is not None:
        payload["top_p"] = request.top_p
    if request.presence_penalty is not None:
        payload["presence_penalty"] = request.presence_penalty
    if request.frequency_penalty is not None:
        payload["frequency_penalty"] = request.frequency_penalty
    if request.n is not None:
        payload["n"] = request.n
    if request.user is not None:
        payload["user"] = request.user
    if request.metadata is not None:
        payload["metadata"] = request.metadata
    if request.tools is not None:
        payload["tools"] = request.tools
    if request.tool_choice is not None:
        payload["tool_choice"] = request.tool_choice
    return payload


# ---------------------------------------------------------------------------
# Telemetry helper for the full flow
# ---------------------------------------------------------------------------


def _extract_task_prompt(request: ProxyRequest) -> str:
    """Extract the first user message as the task prompt for telemetry."""
    for msg in request.messages:
        if msg.role == "user" and msg.content:
            return msg.content
    return ""


async def _write_flow_telemetry(
    vector_store: VectorStore | None,
    request: ProxyRequest,
    phase: str | None,
    composed: bool,
    pre_filter_matched: str | None,
    gates_met: list[str] | None,
    gates_unmet: list[str] | None,
    qwen_calls: int,
    latency_ms: int | None,
    error_code: str | None = None,
    source_skill_ids: list[str] | None = None,
) -> None:
    """Write a telemetry trace for the full proxy request flow."""
    if vector_store is None:
        return
    status = "proxy_composed" if composed else "proxy_passthrough"
    task_prompt = _extract_task_prompt(request)
    write_proxy_trace(
        vector_store,
        phase=phase or "unspecified",
        task_prompt=task_prompt,
        status=status,
        pre_filter_matched=pre_filter_matched,
        gates_met=gates_met or [],
        gates_unmet=gates_unmet or [],
        qwen_calls=qwen_calls,
        total_latency_ms=latency_ms,
        source_skill_ids=source_skill_ids,
        error_code=error_code,
    )


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------


@router.post("/v1/chat/completions", response_model=None)
async def proxy_chat_completions(
    request: ProxyRequest,
    fastapi_request: Request,
    upstream: httpx.AsyncClient | None = Depends(get_upstream_client),
    embed_client: EmbedClient | None = Depends(get_embed_client),
    vector_store: VectorStore | None = Depends(get_vector_store),
    orchestrator: ComposeOrchestrator | None = Depends(get_orchestrator_for_proxy),
    settings: AppSettings = Depends(get_settings_for_proxy),  # pyright: ignore[reportUnknownArgumentType]
):
    """Integrated proxy handler: signal -> compose -> inject -> forward -> telemetry.

    Flow:
    1. Parse ProxyRequest (done by FastAPI body parsing)
    2. Resolve working directory from request metadata or env
    3. Run signal layer (pre-filter + gate evaluation)
    4. If signal matched: run composition and inject into system message
    5. Forward to upstream LLM (streaming or non-streaming)
    6. Write telemetry trace

    Soft-fail: composition failures never block the request — falls through
    to passthrough.
    """
    start_time = time.monotonic()

    if upstream is None:
        return _upstream_not_configured_error()

    # --- Step 1-2: Resolve context ---
    cwd = resolve_working_dir(request)
    phase = read_phase(cwd)

    # --- Step 3: Signal layer ---
    signal_result = None
    composed = False
    try:
        signal_result = await evaluate_signal(request, cwd, embed_client)
    except Exception:
        logger.warning("Signal evaluation failed -- passing through", exc_info=True)

    # --- Step 4: Compose + inject (if signal matched) ---
    modified_request = request
    source_skill_ids: list[str] | None = None
    if signal_result is not None and signal_result.should_compose and orchestrator is not None:
        try:
            modified_request = await compose_and_inject(request, signal_result, orchestrator)
            # Check if injection actually happened (messages differ)
            if modified_request is not request:
                composed = True
        except Exception:
            logger.warning(
                "Composition/injection failed -- passing through unchanged", exc_info=True
            )
            modified_request = request

    # --- Step 5: Forward to upstream ---
    try:
        payload = _build_payload(modified_request, settings.upstream_model)
    except ValueError as e:
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": "upstream_model_not_configured",
                    "message": str(e),
                    "type": "api_error",
                }
            },
        )
    error_code: str | None = None

    if modified_request.stream:
        # Write telemetry after streaming starts (latency tracked separately)
        await _write_flow_telemetry(
            vector_store,
            modified_request,
            phase,
            composed,
            signal_result.pre_filter_matched if signal_result else None,
            signal_result.gates_met if signal_result else None,
            signal_result.gates_unmet if signal_result else None,
            signal_result.qwen_calls if signal_result else 0,
            latency_ms=None,  # streaming latency tracked separately
            source_skill_ids=source_skill_ids,
        )
        return _stream_upstream_response(upstream, payload)

    # Non-streaming: forward and return JSON
    try:
        resp = await upstream.post("/v1/chat/completions", json=payload)
    except httpx.ConnectError as e:
        logger.warning("Upstream connection failed: %s", e)
        error_code = "upstream_connect_error"
        latency_ms = int((time.monotonic() - start_time) * 1000)
        await _write_flow_telemetry(
            vector_store,
            modified_request,
            phase,
            composed,
            signal_result.pre_filter_matched if signal_result else None,
            signal_result.gates_met if signal_result else None,
            signal_result.gates_unmet if signal_result else None,
            signal_result.qwen_calls if signal_result else 0,
            latency_ms=latency_ms,
            error_code=error_code,
            source_skill_ids=source_skill_ids,
        )
        return _upstream_unavailable_error(str(e))
    except httpx.TimeoutException as e:
        logger.warning("Upstream timeout: %s", e)
        error_code = "upstream_timeout"
        latency_ms = int((time.monotonic() - start_time) * 1000)
        await _write_flow_telemetry(
            vector_store,
            modified_request,
            phase,
            composed,
            signal_result.pre_filter_matched if signal_result else None,
            signal_result.gates_met if signal_result else None,
            signal_result.gates_unmet if signal_result else None,
            signal_result.qwen_calls if signal_result else 0,
            latency_ms=latency_ms,
            error_code=error_code,
            source_skill_ids=source_skill_ids,
        )
        return _upstream_unavailable_error(str(e))
    except httpx.HTTPError as e:
        logger.warning("Upstream HTTP error: %s", e)
        error_code = "upstream_http_error"
        latency_ms = int((time.monotonic() - start_time) * 1000)
        await _write_flow_telemetry(
            vector_store,
            modified_request,
            phase,
            composed,
            signal_result.pre_filter_matched if signal_result else None,
            signal_result.gates_met if signal_result else None,
            signal_result.gates_unmet if signal_result else None,
            signal_result.qwen_calls if signal_result else 0,
            latency_ms=latency_ms,
            error_code=error_code,
            source_skill_ids=source_skill_ids,
        )
        return _upstream_unavailable_error(str(e))

    if resp.status_code >= 500:
        logger.warning("Upstream returned HTTP %d: %s", resp.status_code, resp.text[:200])
        error_code = f"upstream_http_{resp.status_code}"
        latency_ms = int((time.monotonic() - start_time) * 1000)
        await _write_flow_telemetry(
            vector_store,
            modified_request,
            phase,
            composed,
            signal_result.pre_filter_matched if signal_result else None,
            signal_result.gates_met if signal_result else None,
            signal_result.gates_unmet if signal_result else None,
            signal_result.qwen_calls if signal_result else 0,
            latency_ms=latency_ms,
            error_code=error_code,
            source_skill_ids=source_skill_ids,
        )
        return _upstream_unavailable_error(f"HTTP {resp.status_code}")

    # Parse and return upstream response
    latency_ms = int((time.monotonic() - start_time) * 1000)
    try:
        body: dict[str, Any] = resp.json()
    except ValueError:
        await _write_flow_telemetry(
            vector_store,
            modified_request,
            phase,
            composed,
            signal_result.pre_filter_matched if signal_result else None,
            signal_result.gates_met if signal_result else None,
            signal_result.gates_unmet if signal_result else None,
            signal_result.qwen_calls if signal_result else 0,
            latency_ms=latency_ms,
            source_skill_ids=source_skill_ids,
        )
        return JSONResponse(
            status_code=resp.status_code,
            content=resp.text,
            media_type=resp.headers.get("content-type", "text/plain"),
        )

    await _write_flow_telemetry(
        vector_store,
        modified_request,
        phase,
        composed,
        signal_result.pre_filter_matched if signal_result else None,
        signal_result.gates_met if signal_result else None,
        signal_result.gates_unmet if signal_result else None,
        signal_result.qwen_calls if signal_result else 0,
        latency_ms=latency_ms,
        source_skill_ids=source_skill_ids,
    )

    return JSONResponse(
        status_code=resp.status_code,
        content=body,
    )


@router.post("/v1/embeddings", response_model=None)
async def proxy_embeddings(
    request: Request,
    embed_async_client: httpx.AsyncClient | None = Depends(get_embed_async_client),
):
    """Forward /v1/embeddings to the embed server."""
    if embed_async_client is None:
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "message": "Embed server not configured",
                    "type": "api_error",
                    "code": "embed_not_configured",
                }
            },
        )

    body = await request.json()
    resp = await embed_async_client.post("/v1/embeddings", json=body)

    return JSONResponse(
        status_code=resp.status_code,
        content=resp.json(),
    )
