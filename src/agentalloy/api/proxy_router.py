"""Proxy router — forwards chat completions to the upstream LLM.

Handles both non-streaming (JSON) and streaming (SSE) responses.
Signal evaluation, composition injection, and telemetry are added in
later tasks.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from agentalloy.api.proxy_models import ProxyRequest

logger = logging.getLogger(__name__)

router = APIRouter()


def get_upstream_client(request: Request) -> httpx.AsyncClient | None:
    """Return the upstream LLM httpx.AsyncClient (lifespan-scoped, via app.state).

    Returns None if the upstream is not configured.
    """
    return getattr(request.app.state, "upstream_client", None)


def _upstream_not_configured_error() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "error": {
                "code": "upstream_not_configured",
                "message": "Upstream LLM is not configured. Set UPSTREAM_URL, UPSTREAM_MODEL, and UPSTREAM_API_KEY.",
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


@router.post("/v1/chat/completions", response_model=None)
async def proxy_chat_completions(
    request: ProxyRequest,
    upstream: httpx.AsyncClient | None = Depends(get_upstream_client),
):
    """Forward a chat completion request to the upstream LLM.

    Receives an OpenAI-compatible request body, forwards it to the
    upstream LLM's /v1/chat/completions endpoint, and returns the
    response unchanged. Supports both streaming and non-streaming modes.
    """
    if upstream is None:
        return _upstream_not_configured_error()

    # Build the payload from the ProxyRequest, preserving all fields
    payload: dict[str, Any] = {
        "model": request.model,
        "messages": [m.model_dump() for m in request.messages],
        "stream": request.stream,
    }
    # Forward optional parameters
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

    # Streaming mode: forward SSE chunks
    if request.stream:
        return _stream_upstream_response(upstream, payload)

    # Non-streaming mode: forward and return JSON
    try:
        resp = await upstream.post("/v1/chat/completions", json=payload)
    except httpx.ConnectError as e:
        logger.warning("Upstream connection failed: %s", e)
        return _upstream_unavailable_error(str(e))
    except httpx.TimeoutException as e:
        logger.warning("Upstream timeout: %s", e)
        return _upstream_unavailable_error(str(e))
    except httpx.HTTPError as e:
        logger.warning("Upstream HTTP error: %s", e)
        return _upstream_unavailable_error(str(e))

    if resp.status_code >= 500:
        logger.warning("Upstream returned HTTP %d: %s", resp.status_code, resp.text[:200])
        return _upstream_unavailable_error(f"HTTP {resp.status_code}")

    # Parse and return upstream response
    try:
        body: dict[str, Any] = resp.json()
    except ValueError:
        # Non-JSON response — return as-is
        return JSONResponse(
            status_code=resp.status_code,
            content=resp.text,
            media_type=resp.headers.get("content-type", "text/plain"),
        )

    return JSONResponse(
        status_code=resp.status_code,
        content=body,
    )
