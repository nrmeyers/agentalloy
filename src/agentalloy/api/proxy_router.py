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
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from agentalloy.api.proxy_context import decode_proj_token, read_phase, resolve_working_dir
from agentalloy.api.proxy_injection import compose_and_inject
from agentalloy.api.proxy_models import ProxyRequest
from agentalloy.api.proxy_session import extract_session_header, resolve_session_key
from agentalloy.api.proxy_signal import evaluate_signal
from agentalloy.api.proxy_telemetry import write_proxy_trace
from agentalloy.api.upstream.error_sse import error_sse_plain

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
        try:
            async with upstream.stream("POST", "/v1/chat/completions", json=payload) as resp:
                if resp.status_code >= 500:
                    logger.warning("Upstream streaming returned HTTP %d", resp.status_code)
                    yield error_sse_plain(
                        f"Upstream returned HTTP {resp.status_code}", resp.status_code
                    )
                    return
                async for chunk in resp.aiter_text():
                    yield chunk
        except httpx.HTTPStatusError as exc:
            logger.warning("Upstream streaming HTTP status error: %s", exc)
            yield error_sse_plain(f"Upstream HTTP error: {exc}", exc.response.status_code)
        except httpx.ConnectError as exc:
            logger.warning("Upstream streaming connection failed: %s", exc)
            yield error_sse_plain(f"Upstream connection failed: {exc}")
        except httpx.TimeoutException as exc:
            logger.warning("Upstream streaming timed out: %s", exc)
            yield error_sse_plain(f"Upstream timeout: {exc}")
        except httpx.RequestError as exc:
            logger.warning("Upstream streaming request error: %s", exc)
            yield error_sse_plain(f"Upstream request error: {exc}")
        except httpx.HTTPError as exc:
            logger.warning("Upstream streaming HTTP error: %s", exc)
            yield error_sse_plain(f"Upstream HTTP error: {exc}")
        except Exception as exc:
            logger.warning("Upstream streaming unexpected error: %s", exc, exc_info=True)
            yield error_sse_plain(f"Upstream error: {exc}")

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
    """Extract the first user message as the task prompt for telemetry.

    ``ProxyMessage.content`` is ``str | list[dict[str, Any]] | None`` — the
    list form carries Anthropic-style content blocks. For telemetry we want
    a plain string, so flatten any blocks by concatenating their ``text``
    fields and skip non-text blocks.
    """
    for msg in request.messages:
        if msg.role != "user" or not msg.content:
            continue
        if isinstance(msg.content, str):
            return msg.content
        # list of content blocks
        parts = [block.get("text", "") for block in msg.content if block.get("type") == "text"]
        joined = "".join(parts)
        if joined:
            return joined
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
    phase_gate_embed_failed: bool = False,
    repo: str | None = None,
    session_key: str | None = None,
    session_source: str | None = None,
) -> None:
    """Write a telemetry trace for the full proxy request flow.

    ``repo`` and ``session_*`` are the values the handler already resolved (the
    handler may have used a /proj/<token> override, so they're passed in rather
    than recomputed here).
    """
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
        session_key=session_key,
        session_source=session_source,
        phase_gate_embed_failed=phase_gate_embed_failed,
        repo=repo,
    )


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------


@router.post("/v1/chat/completions", response_model=None)
@router.post("/proj/{token}/v1/chat/completions", response_model=None)
async def proxy_chat_completions(
    request: ProxyRequest,
    fastapi_request: Request,
    token: str | None = None,
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
    # When the OpenAI base URL carries a /proj/<token> discriminator (the same
    # realpath-baked token the Anthropic passthrough uses), decode it to the repo;
    # otherwise fall back to the metadata.cwd / env / process-cwd chain.
    cwd_override: Path | None = None
    if token:
        try:
            cwd_override = decode_proj_token(token)
        except ValueError:
            cwd_override = None
    cwd = resolve_working_dir(request, cwd_override)
    phase = read_phase(cwd)
    repo = str(cwd)

    # Per-session orientation key: explicit harness header (e.g. Claude Code's
    # x-claude-code-session-id) else the conversation fingerprint. Drives the
    # announce cadence and is stamped onto telemetry.
    session_id = extract_session_header(fastapi_request.headers)
    session_key, session_source = resolve_session_key(request, session_id)

    # --- Step 3: Signal layer ---
    signal_result = None
    composed = False
    try:
        signal_result = await evaluate_signal(request, cwd, embed_client, session_id)
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

    # No `commit_markers` here by design. This OpenAI-translation path injects only
    # domain skills + eval advisories via `compose_and_inject`; it does not emit the
    # Tier 1 orientation block or the per-contract Tier 2 cursor block, so it owns
    # neither the `announced` nor the `composed` cadence. `evaluate_signal` no longer
    # writes those markers either, so this path simply never touches them — the
    # announce/cursor cadence is committed only on the Anthropic passthrough path
    # that actually delivers those blocks. (OpenAI-harness orientation parity is a
    # separate, tracked gap.)

    # Carry the phase-gate embed-failure flag into every telemetry write below
    # (computed once; the value is the same for all exit paths of this request).
    gate_embed_failed = signal_result.phase_gate_embed_failed if signal_result else False

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
            phase_gate_embed_failed=gate_embed_failed,
            repo=repo,
            session_key=session_key,
            session_source=session_source,
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
            phase_gate_embed_failed=gate_embed_failed,
            repo=repo,
            session_key=session_key,
            session_source=session_source,
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
            phase_gate_embed_failed=gate_embed_failed,
            repo=repo,
            session_key=session_key,
            session_source=session_source,
        )
        return _upstream_unavailable_error(str(e))
    except httpx.RequestError as e:
        logger.warning("Upstream request error: %s", e)
        error_code = "upstream_request_error"
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
            phase_gate_embed_failed=gate_embed_failed,
            repo=repo,
            session_key=session_key,
            session_source=session_source,
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
            phase_gate_embed_failed=gate_embed_failed,
            repo=repo,
            session_key=session_key,
            session_source=session_source,
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
            phase_gate_embed_failed=gate_embed_failed,
            repo=repo,
            session_key=session_key,
            session_source=session_source,
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
            phase_gate_embed_failed=gate_embed_failed,
            repo=repo,
            session_key=session_key,
            session_source=session_source,
        )
        # Raw passthrough: Response does not re-encode, so a non-JSON upstream
        # body is forwarded verbatim with its original Content-Type (JSONResponse
        # would json.dumps() the text, double-encoding it).
        return Response(
            content=resp.text,
            status_code=resp.status_code,
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
        phase_gate_embed_failed=gate_embed_failed,
        repo=repo,
        session_key=session_key,
        session_source=session_source,
    )

    return JSONResponse(
        status_code=resp.status_code,
        content=body,
    )


@router.post("/v1/embeddings", response_model=None)
@router.post("/proj/{token}/v1/embeddings", response_model=None)
async def proxy_embeddings(
    request: Request,
    token: str | None = None,
    embed_async_client: httpx.AsyncClient | None = Depends(get_embed_async_client),
):
    """Forward /v1/embeddings to the embed server.

    The ``/proj/<token>`` variant exists so an OpenAI harness wired with a
    ``.../proj/<token>/v1`` base URL reaches embeddings too (the token is
    irrelevant here — embeddings carry no repo context — but the path must match).
    """
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
    try:
        resp = await embed_async_client.post("/v1/embeddings", json=body)
    except httpx.ConnectError as e:
        logger.warning("Embed server connection failed: %s", e)
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "message": f"Embed server unavailable: {e}",
                    "type": "api_error",
                    "code": "embed_connection_error",
                }
            },
        )
    except httpx.TimeoutException as e:
        logger.warning("Embed server timeout: %s", e)
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "message": f"Embed server timeout: {e}",
                    "type": "api_error",
                    "code": "embed_timeout",
                }
            },
        )
    except httpx.HTTPError as e:
        logger.warning("Embed server HTTP error: %s", e)
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "message": f"Embed server HTTP error: {e}",
                    "type": "api_error",
                    "code": "embed_http_error",
                }
            },
        )

    try:
        body = resp.json()
    except ValueError:
        # Non-JSON body (e.g. an HTML 502 from a reverse proxy) — pass through
        # verbatim instead of raising an unhandled JSONDecodeError -> bare 500.
        return Response(
            content=resp.text,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "text/plain"),
        )
    return JSONResponse(
        status_code=resp.status_code,
        content=body,
    )
