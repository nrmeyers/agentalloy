"""Native OpenAI Responses passthrough (the ``/proj/<token>/v1/responses`` path).

The Responses-API sibling of ``proxy_passthrough_router`` (codex et al. —
modern codex speaks ONLY this wire). No Responses↔ChatCompletions translation:

1. decode the ``/proj/<token>`` discriminator → the per-repo project dir,
2. run the signal layer + compose engine for that repo's phase,
3. inject the composed prose into the **last user message input item** (the
   top-level ``instructions`` field is the harness's cached system prompt and
   stays byte-identical),
4. forward the request **verbatim** to a Responses-capable upstream
   (``RESPONSES_UPSTREAM_URL``, default api.openai.com), carrying the caller's
   own credential, and relay the response (raw SSE byte relay when streaming).

Every step before the forward soft-fails to forwarding the ORIGINAL request
unchanged. Spec: docs/responses-surface.md.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse

from agentalloy.api.anthropic_passthrough import AnthropicPassthroughClient
from agentalloy.api.proxy_apply import InjectOutcome, apply_signal
from agentalloy.api.proxy_context import decode_proj_token
from agentalloy.api.proxy_injection import inject_into_responses_input
from agentalloy.api.proxy_models import ProxyMessage, ProxyRequest
from agentalloy.api.proxy_passthrough_router import (
    _forward_once,  # pyright: ignore[reportPrivateUsage]
    _forward_streaming,  # pyright: ignore[reportPrivateUsage]
    _make_on_status,  # pyright: ignore[reportPrivateUsage]
    _noop_status,  # pyright: ignore[reportPrivateUsage]
)
from agentalloy.api.proxy_router import (
    get_embed_client,
    get_orchestrator_for_proxy,
    get_vector_store,
)
from agentalloy.api.proxy_session import extract_session_header
from agentalloy.api.proxy_signal import SignalResult, evaluate_signal

if TYPE_CHECKING:
    from agentalloy.embed_provider import EmbedClient
    from agentalloy.orchestration.compose import ComposeOrchestrator
    from agentalloy.storage.protocols import TelemetryStore

logger = logging.getLogger(__name__)

router = APIRouter()

_UPSTREAM_PATH = "/v1/responses"


def get_responses_client(request: Request) -> AnthropicPassthroughClient | None:
    """Return the lifespan-scoped Responses passthrough client from app.state."""
    return getattr(request.app.state, "responses_passthrough_client", None)


def _item_text(content: Any) -> str | None:
    """Flatten a Responses message item's content to text for the signal layer."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for raw in cast("list[Any]", content):
            if isinstance(raw, dict):
                block = cast("dict[str, Any]", raw)
                text = block.get("text")
                if block.get("type") in ("input_text", "output_text") and isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts) if parts else None
    return None


def _proxy_request_from_responses(payload: dict[str, Any]) -> ProxyRequest:
    """Build a minimal ProxyRequest for the signal layer from a Responses payload.

    Maps ``input`` message items → ``ProxyMessage`` (input_text/output_text
    blocks flattened to text) and the top-level ``tools`` array →
    ``ProxyRequest.tools`` (the carrier gate distinguishes a real agent turn
    from a background micro-request by its presence). A bare-string ``input``
    becomes a single user message.
    """
    messages: list[ProxyMessage] = []
    raw_input = payload.get("input")
    if isinstance(raw_input, str):
        messages.append(ProxyMessage(role="user", content=raw_input))
    elif isinstance(raw_input, list):
        for raw in cast("list[Any]", raw_input):
            if not isinstance(raw, dict):
                continue
            item = cast("dict[str, Any]", raw)
            if item.get("type") != "message":
                continue
            role = item.get("role")
            if role not in ("user", "assistant", "system"):
                continue
            messages.append(ProxyMessage(role=role, content=_item_text(item.get("content"))))
    model = payload.get("model")
    raw_tools = payload.get("tools")
    tools = cast("list[dict[str, Any]]", raw_tools) if isinstance(raw_tools, list) else None
    return ProxyRequest(
        model=model if isinstance(model, str) else "unknown",
        messages=messages,
        tools=tools,
    )


async def _maybe_inject(
    payload: dict[str, Any],
    token: str,
    embed_client: EmbedClient | None,
    orchestrator: ComposeOrchestrator | None,
    session_id: str | None = None,
) -> tuple[dict[str, Any] | None, InjectOutcome[dict[str, Any]] | None, SignalResult]:
    """Run signal → compose → inject for this repo (Responses payload shape).

    Mirrors the Anthropic passthrough's ``_maybe_inject``; see that docstring
    for the cadence-marker and identity-equals-delivered contracts.
    """
    project_dir = decode_proj_token(token)  # ValueError on a bad token → caller soft-fails
    signal = await evaluate_signal(
        _proxy_request_from_responses(payload), project_dir, embed_client, session_id
    )

    current = payload
    outcome: InjectOutcome[dict[str, Any]] | None = None

    if signal.should_compose and signal.phase and orchestrator is not None:
        phase = signal.phase
        before = current
        outcome = await apply_signal(
            signal=signal,
            orchestrator=orchestrator,
            inject=lambda text: inject_into_responses_input(before, text, phase=phase),
            delivered=lambda out: out is not before,
        )
        if outcome.injected is not None:
            current = outcome.injected

    if signal.banner is not None and signal.phase is not None:
        bannered = inject_into_responses_input(
            current, signal.banner, phase=signal.phase, kind="banner"
        )
        if bannered is not current:
            current = bannered

    injected_payload = current if current is not payload else None
    return injected_payload, outcome, signal


@router.post("/proj/{token}/v1/responses", response_model=None)
async def passthrough_openai_responses(
    token: str,
    request: Request,
    client: AnthropicPassthroughClient | None = Depends(get_responses_client),
    embed_client: EmbedClient | None = Depends(get_embed_client),
    orchestrator: ComposeOrchestrator | None = Depends(get_orchestrator_for_proxy),
    vector_store: TelemetryStore | None = Depends(get_vector_store),
) -> Response | StreamingResponse:
    raw_body = await request.body()
    query_string = request.url.query
    inbound_headers = request.headers

    if client is None:
        return Response(
            content=json.dumps(
                {
                    "error": {
                        "type": "api_error",
                        "message": "responses passthrough upstream not configured",
                    }
                }
            ).encode(),
            status_code=503,
            media_type="application/json",
        )

    # --- Pre-forward: compose + inject, soft-failing to the original body. ---
    body_to_send = raw_body
    stream_flag = False
    payload: dict[str, Any] | None = None
    try:
        parsed: Any = json.loads(raw_body)
        if isinstance(parsed, dict):
            payload = cast("dict[str, Any]", parsed)
            stream_flag = bool(payload.get("stream", False))
    except Exception:
        payload = None  # not JSON — forward verbatim

    on_status: Callable[[int], None] = _noop_status
    if payload is not None:
        try:
            session_id = extract_session_header(inbound_headers)
            injected, outcome, signal = await _maybe_inject(
                payload, token, embed_client, orchestrator, session_id
            )
            if injected is not None:
                body_to_send = json.dumps(injected).encode("utf-8")
            on_status = _make_on_status(decode_proj_token(token), outcome, vector_store, signal)
        except Exception:
            logger.warning(
                "responses passthrough compose/inject failed; forwarding original", exc_info=True
            )
            body_to_send = raw_body

    # --- Forward (shared with the Anthropic passthrough; only the path differs). ---
    if stream_flag:
        return await _forward_streaming(
            client, query_string, inbound_headers, body_to_send, on_status, path=_UPSTREAM_PATH
        )
    return await _forward_once(
        client, query_string, inbound_headers, body_to_send, on_status, path=_UPSTREAM_PATH
    )
