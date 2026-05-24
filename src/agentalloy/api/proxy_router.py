"""Proxy router — forwards chat completions to the upstream LLM.

Basic passthrough: receives an OpenAI-compatible request, forwards it to
the upstream LLM, and returns the response unchanged. Signal evaluation,
composition injection, and streaming are added in later tasks.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from agentalloy.api.proxy_models import ProxyRequest

logger = logging.getLogger(__name__)

router = APIRouter()


def get_upstream_client(request: Request) -> httpx.Client | None:
    """Return the upstream LLM httpx client (lifespan-scoped, via app.state).

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


@router.post("/v1/chat/completions")
def proxy_chat_completions(
    request: ProxyRequest,
    upstream: httpx.Client | None = Depends(get_upstream_client),
) -> JSONResponse:
    """Forward a chat completion request to the upstream LLM.

    Receives an OpenAI-compatible request body, forwards it to the
    upstream LLM's /v1/chat/completions endpoint, and returns the
    response unchanged.
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

    try:
        resp = upstream.post("/v1/chat/completions", json=payload)
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
