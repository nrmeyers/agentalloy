"""Anthropic Messages API proxy router.

Translates POST /v1/messages (Anthropic format) to the existing OpenAI-compatible
proxy pipeline and converts the response back to Anthropic format.

Phase 1 scope: text-only, non-streaming and streaming.  Tool use / function
calling is out of scope — tool_calls deltas are silently stripped.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from agentalloy.api.proxy_anthropic_models import (
    AnthropicContentBlock,
    AnthropicRequest,
    AnthropicResponse,
)
from agentalloy.api.proxy_models import ProxyMessage, ProxyRequest
from agentalloy.api.proxy_router import (  # pyright: ignore[reportPrivateUsage]
    _build_payload,
    _stream_upstream_response,
    _upstream_not_configured_error,
    get_settings_for_proxy,
    get_upstream_client,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Translation helpers
# ---------------------------------------------------------------------------


def _anthropic_to_openai(request: AnthropicRequest) -> ProxyRequest:
    """Convert an Anthropic Messages request to an OpenAI ProxyRequest.

    - ``system``: string → ``{"role": "system", "content": system}`` prepended
    - ``messages``: role/content pass-through (already compatible)
    - ``stream``, ``temperature``, ``top_p``: pass-through
    - ``model``: pass-through (proxy handles model resolution)
    - ``max_tokens``: mapped to ``max_tokens`` on ProxyRequest
    """
    messages: list[ProxyMessage] = []
    if request.system:
        messages.append(ProxyMessage(role="system", content=request.system))
    for m in request.messages:
        messages.append(ProxyMessage(role=m.role, content=m.content))

    return ProxyRequest(
        model=request.model,
        messages=messages,
        stream=request.stream,
        temperature=request.temperature,
        max_tokens=request.max_tokens,
        top_p=request.top_p,
    )


def _openai_to_anthropic(openai_body: dict[str, Any], model: str) -> dict[str, Any]:
    """Convert a non-streaming OpenAI chat completion response to Anthropic format.

    - ``choices[0].message.content`` → ``content: [{type: "text", text: ...}]``
    - ``usage.prompt_tokens`` / ``completion_tokens`` → ``usage.input_tokens`` / ``output_tokens``
    - ``finish_reason`` → ``stop_reason`` (``"stop"`` → ``"end_turn"``,
      ``"length"`` → ``"max_tokens"``)
    """
    choices: list[dict[str, Any]] = openai_body.get("choices") or [{}]
    choice: dict[str, Any] = choices[0]
    message: dict[str, Any] = choice.get("message") or {}
    text: str = message.get("content") or ""

    finish: str | None = choice.get("finish_reason")
    stop_reason: str | None = None
    if finish == "stop":
        stop_reason = "end_turn"
    elif finish == "length":
        stop_reason = "max_tokens"

    usage_raw: dict[str, Any] = openai_body.get("usage") or {}
    usage: dict[str, Any] = {
        "input_tokens": usage_raw.get("prompt_tokens", 0),
        "output_tokens": usage_raw.get("completion_tokens", 0),
    }

    response = AnthropicResponse(
        id=openai_body.get("id") or f"msg_{uuid.uuid4().hex[:24]}",
        content=[AnthropicContentBlock(text=text)],
        model=openai_body.get("model") or model,
        stop_reason=stop_reason,
        stop_sequence=None,
        usage=usage,
    )
    return response.model_dump()


def _openai_stream_to_anthropic(
    openai_chunks: list[dict[str, Any]], model: str
) -> list[dict[str, Any]]:
    """Convert a sequence of OpenAI SSE chunks to Anthropic SSE events.

    Mapping:
    - First chunk → ``message_start`` + ``content_block_start``
    - Text content chunks → ``content_block_delta``
    - Last chunk (finish_reason set) → ``content_block_stop``
      + ``message_delta`` (with output_tokens) + ``message_stop``

    Tool calls are stripped (text-only mode).

    Usage note: in Anthropic streaming, usage goes in ``message_delta``,
    NOT in ``message_stop``.
    """
    events: list[dict[str, Any]] = []
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    output_tokens: int = 0
    input_tokens: int = 0
    first = True
    stop_reason = "end_turn"

    for chunk in openai_chunks:
        choices: list[dict[str, Any]] = chunk.get("choices") or []
        if not choices:
            usage: dict[str, Any] = chunk.get("usage") or {}
            if usage:
                input_tokens = int(usage.get("prompt_tokens") or input_tokens)
                output_tokens = int(usage.get("completion_tokens") or output_tokens)
            continue

        choice: dict[str, Any] = choices[0]
        delta: dict[str, Any] = choice.get("delta") or {}
        finish: str | None = choice.get("finish_reason")

        # Strip tool_calls — text-only mode
        if delta.get("tool_calls"):
            logger.warning("Anthropic router received tool_calls; stripping (text-only mode)")

        text: str = delta.get("content") or ""

        if first:
            events.append(
                {
                    "type": "message_start",
                    "message": {
                        "id": msg_id,
                        "type": "message",
                        "role": "assistant",
                        "content": [],
                        "model": model,
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 0, "output_tokens": 0},
                    },
                }
            )
            events.append(
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                }
            )
            first = False

        if text:
            events.append(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": text},
                }
            )

        if finish:
            stop_reason = "end_turn" if finish == "stop" else "max_tokens"

        usage: dict[str, Any] = chunk.get("usage") or {}
        if usage:
            input_tokens = int(usage.get("prompt_tokens") or input_tokens)
            output_tokens = int(usage.get("completion_tokens") or output_tokens)

    if first:
        # Empty stream
        events.append(
            {
                "type": "message_start",
                "message": {
                    "id": msg_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": model,
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            }
        )
        events.append(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            }
        )

    events.append({"type": "content_block_stop", "index": 0})
    events.append(
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": output_tokens},
        }
    )
    events.append({"type": "message_stop"})
    return events


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------


@router.post("/v1/messages", response_model=None)
async def proxy_anthropic_messages(
    request: AnthropicRequest,
    http_request: Request,
    upstream: httpx.AsyncClient | None = Depends(get_upstream_client),
    settings: Any = Depends(get_settings_for_proxy),
) -> JSONResponse | StreamingResponse:
    """Proxy Anthropic Messages API requests through the AgentAlloy pipeline.

    1. Convert Anthropic request → OpenAI ProxyRequest
    2. Build upstream payload (with model resolution)
    3. Forward to upstream and convert response back to Anthropic format
    """
    if upstream is None:
        return _upstream_not_configured_error()

    openai_request = _anthropic_to_openai(request)
    payload = _build_payload(openai_request, settings.upstream_model)

    if request.stream:
        return _stream_upstream_response(upstream, payload)

    # Non-streaming
    try:
        resp = await upstream.post("/v1/chat/completions", json=payload)
    except httpx.ConnectError as e:
        logger.warning("Upstream connection failed: %s", e)
        return JSONResponse(
            status_code=503,
            content={
                "type": "error",
                "error": {"type": "overloaded_error", "message": f"Upstream unavailable: {e}"},
            },
        )

    if resp.status_code != 200:
        return JSONResponse(
            status_code=resp.status_code,
            content={"type": "error", "error": {"type": "api_error", "message": resp.text}},
        )

    openai_body: dict[str, Any] = resp.json()
    anthropic_body = _openai_to_anthropic(openai_body, request.model)
    return JSONResponse(content=anthropic_body)
