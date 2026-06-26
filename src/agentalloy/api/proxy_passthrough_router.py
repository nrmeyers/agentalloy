"""Native Anthropic Messages passthrough (the ``/proj/<token>/v1/messages`` path).

This path does **no** Anthropic↔OpenAI translation. It:

1. decodes the ``/proj/<token>`` discriminator → the per-repo project dir,
2. runs the signal layer + compose engine for that repo's phase,
3. injects the composed prose into the **last user message** (the top-level
   ``system`` block is left byte-identical so prompt caching is preserved),
4. forwards the request **verbatim** to a configurable Anthropic upstream,
   carrying the caller's own credential, and relays the response (raw SSE byte
   relay when streaming).

Every step before the forward is wrapped so that any failure falls back to
forwarding the **original** request unchanged — composition never blocks the
proxy. Auth is transparent: this path holds no Anthropic credential.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import httpx
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse

from agentalloy.api.anthropic_passthrough import AnthropicPassthroughClient
from agentalloy.api.proxy_apply import (
    InjectOutcome,
    _compose_block,  # pyright: ignore[reportPrivateUsage]  # noqa: F401 — re-exported for callers/tests
    _ComposedBlock,  # pyright: ignore[reportPrivateUsage]  # noqa: F401 — re-exported for callers/tests
    apply_signal,
    commit_outcome,
)
from agentalloy.api.proxy_context import decode_proj_token
from agentalloy.api.proxy_injection import inject_into_anthropic_messages
from agentalloy.api.proxy_models import ProxyMessage, ProxyRequest
from agentalloy.api.proxy_router import get_embed_client, get_orchestrator_for_proxy
from agentalloy.api.proxy_session import extract_session_header
from agentalloy.api.proxy_signal import evaluate_signal

if TYPE_CHECKING:
    from agentalloy.embed_provider import EmbedClient
    from agentalloy.orchestration.compose import ComposeOrchestrator

logger = logging.getLogger(__name__)

router = APIRouter()

# Re-exported from proxy_apply so existing imports of these symbols from this
# module keep working; the implementations live in the shared seam.
__all__ = ["_ComposedBlock", "_compose_block", "router"]

# Upstream path the discriminator maps to (the /proj/<token> prefix is ours).
_UPSTREAM_PATH = "/v1/messages"

# Response headers we never relay back to the client.
_RESPONSE_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)


def get_passthrough_client(request: Request) -> AnthropicPassthroughClient | None:
    """Return the lifespan-scoped passthrough client from app.state."""
    return getattr(request.app.state, "anthropic_passthrough_client", None)


def _proxy_request_from_anthropic(payload: dict[str, Any]) -> ProxyRequest:
    """Build a minimal ProxyRequest for the signal layer.

    The signal layer reads user-message text (to derive the task prompt) and the
    presence of a tool array (to tell a real agent turn from a background
    micro-request — see the carrier gate in ``evaluate_signal``). Anthropic message
    content (str or a list of content blocks) maps straight onto
    ``ProxyMessage.content``; the top-level Anthropic ``tools`` array maps onto
    ``ProxyRequest.tools``. The top-level Anthropic ``system`` field is
    intentionally ignored here.
    """
    messages: list[ProxyMessage] = []
    raw_messages = payload.get("messages")
    if isinstance(raw_messages, list):
        for raw in cast("list[Any]", raw_messages):
            if not isinstance(raw, dict):
                continue
            m = cast("dict[str, Any]", raw)
            role = m.get("role")
            if role not in ("user", "assistant", "system", "tool"):
                continue
            content = m.get("content")
            usable = cast(
                "str | list[dict[str, Any]] | None",
                content if isinstance(content, (str, list)) else None,
            )
            messages.append(ProxyMessage(role=role, content=usable))
    model = payload.get("model")
    raw_tools = payload.get("tools")
    tools = cast("list[dict[str, Any]]", raw_tools) if isinstance(raw_tools, list) else None
    return ProxyRequest(
        model=model if isinstance(model, str) else "unknown",
        messages=messages,
        tools=tools,
    )


def _noop_status(_status: int) -> None:
    """Default ``on_status`` for the verbatim-forward path (nothing composed)."""
    return None


def _commit_on_2xx(
    project_dir: Path, outcome: InjectOutcome[dict[str, Any]]
) -> Callable[[int], None]:
    """Build an ``on_status`` that commits ``outcome``'s markers iff status is 2xx."""

    def on_status(status: int) -> None:
        commit_outcome(project_dir, outcome, upstream_ok=200 <= status < 300)

    return on_status


async def _maybe_inject(
    payload: dict[str, Any],
    token: str,
    embed_client: EmbedClient | None,
    orchestrator: ComposeOrchestrator | None,
    session_id: str | None = None,
) -> tuple[dict[str, Any] | None, InjectOutcome[dict[str, Any]] | None]:
    """Run signal → compose → inject for this repo.

    Returns ``(payload_or_None, outcome_or_None)``: the new payload (None when
    nothing was injected — skip / no-op), and the :class:`InjectOutcome` whose
    cadence markers the caller commits *after a 2xx forward* (None when no workflow
    block was composed). Raising is fine — the caller treats any exception as
    "forward the original unchanged". ``session_id`` is the harness session-id
    header (Claude Code's ``x-claude-code-session-id``),
    used to key per-session orientation.
    """
    project_dir = decode_proj_token(token)  # ValueError on a bad token → caller soft-fails
    signal = await evaluate_signal(
        _proxy_request_from_anthropic(payload), project_dir, embed_client, session_id
    )

    # Two independent injections, both landing in the last user message:
    #   1. the workflow/cursor block (gated on should_compose), and
    #   2. the per-turn phase banner (signal.banner), which fires on EVERY carrier turn
    #      even when no workflow block is composed.
    # The banner injects AFTER the workflow block so it is the freshest text. We track
    # the latest payload across both and return it iff anything was injected (else None
    # → the caller forwards the original verbatim).
    current = payload
    outcome: InjectOutcome[dict[str, Any]] | None = None

    # 1. Workflow/cursor block via the shared seam (cadence-marker committing).
    if signal.should_compose and signal.phase and orchestrator is not None:
        # Cadence lives in `.agentalloy/{announced,composed}` (durable), not in the
        # request body. The signal layer decided this turn warrants injection but
        # deliberately did NOT commit the markers — `apply_signal` defers that to
        # `commit_outcome`, which the caller runs only after a 2xx forward, so a
        # degraded compose (embed down), an empty block, OR a turn the model never
        # processed (overloaded/errored upstream) never records the phase/work-item
        # as delivered.
        #
        # `inject_into_anthropic_messages` returns a NEW dict on a real injection and
        # the SAME object on every no-op (no user message, already-present marker,
        # malformed/unknown content shape). Identity, not None-ness, proves the block
        # reached the request — so `delivered` is the identity test and a turn that
        # composed text but couldn't inject it does NOT burn the marker.
        phase = signal.phase
        before = current
        outcome = await apply_signal(
            signal=signal,
            orchestrator=orchestrator,
            inject=lambda text: inject_into_anthropic_messages(before, text, phase=phase),
            delivered=lambda out: out is not before,
        )
        if outcome.injected is not None:
            current = outcome.injected

    # 2. Per-turn banner — strip-and-replace, appended LAST so it is the freshest text.
    #    Carrier-gated upstream: evaluate_signal only sets `banner` on a carrier turn,
    #    so a tool-less background request gets banner=None and injects nothing here.
    #    Independent of should_compose: it fires even on a banner-only turn.
    if signal.banner is not None and signal.phase is not None:
        bannered = inject_into_anthropic_messages(
            current, signal.banner, phase=signal.phase, kind="banner"
        )
        if bannered is not current:
            current = bannered

    injected_payload = current if current is not payload else None
    return injected_payload, outcome


def _response_headers(headers: httpx.Headers, *, decoded_body: bool) -> dict[str, str]:
    """Filter upstream response headers for relay. Drops hop-by-hop, length, and
    (when the body was decoded by httpx) the now-wrong content-encoding. The
    content-type is relayed separately via ``media_type``."""
    out: dict[str, str] = {}
    for k, v in headers.items():
        kl = k.lower()
        if kl in _RESPONSE_HOP or kl in ("content-length", "content-type"):
            continue
        if decoded_body and kl == "content-encoding":
            continue
        out[k] = v
    return out


@router.post("/proj/{token}/v1/messages", response_model=None)
async def passthrough_anthropic_messages(
    token: str,
    request: Request,
    client: AnthropicPassthroughClient | None = Depends(get_passthrough_client),
    embed_client: EmbedClient | None = Depends(get_embed_client),
    orchestrator: ComposeOrchestrator | None = Depends(get_orchestrator_for_proxy),
) -> Response | StreamingResponse:
    raw_body = await request.body()
    query_string = request.url.query
    inbound_headers = request.headers

    if client is None:
        return Response(
            content=json.dumps(
                {
                    "type": "error",
                    "error": {
                        "type": "api_error",
                        "message": "passthrough upstream not configured",
                    },
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

    # `on_status` commits the deferred cadence markers, but only on a 2xx forward —
    # so an orientation block injected into a request that upstream then 529s/errors
    # is NOT recorded as delivered, and re-fires on the harness retry. Default no-op
    # covers the verbatim-forward path (nothing composed).
    on_status: Callable[[int], None] = _noop_status
    if payload is not None:
        try:
            session_id = extract_session_header(inbound_headers)
            injected, outcome = await _maybe_inject(
                payload, token, embed_client, orchestrator, session_id
            )
            if injected is not None:
                body_to_send = json.dumps(injected).encode("utf-8")
            if outcome is not None:
                on_status = _commit_on_2xx(decode_proj_token(token), outcome)
        except Exception:
            logger.warning("passthrough compose/inject failed; forwarding original", exc_info=True)
            body_to_send = raw_body

    # --- Forward. ---
    if stream_flag:
        return await _forward_streaming(
            client, query_string, inbound_headers, body_to_send, on_status
        )
    return await _forward_once(client, query_string, inbound_headers, body_to_send, on_status)


async def _forward_once(
    client: AnthropicPassthroughClient,
    query_string: str,
    inbound_headers: Any,
    body: bytes,
    on_status: Callable[[int], None] = lambda _status: None,
) -> Response:
    try:
        upstream = await client.forward(
            path=_UPSTREAM_PATH,
            query_string=query_string,
            inbound_headers=inbound_headers,
            body=body,
        )
    except httpx.HTTPError as e:
        logger.warning("passthrough upstream error: %s", e)
        # No commit: a connection-level failure means the model never saw the block.
        return Response(
            content=json.dumps(
                {"type": "error", "error": {"type": "api_error", "message": f"upstream error: {e}"}}
            ).encode(),
            status_code=502,
            media_type="application/json",
        )
    on_status(upstream.status_code)
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=_response_headers(upstream.headers, decoded_body=True),
        media_type=upstream.headers.get("content-type"),
    )


async def _forward_streaming(
    client: AnthropicPassthroughClient,
    query_string: str,
    inbound_headers: Any,
    body: bytes,
    on_status: Callable[[int], None] = lambda _status: None,
) -> Response | StreamingResponse:
    # Enter the stream manually so we can read the upstream status + headers
    # before constructing the StreamingResponse, then relay raw bytes.
    cm = client.stream(
        path=_UPSTREAM_PATH,
        query_string=query_string,
        inbound_headers=inbound_headers,
        body=body,
    )
    try:
        upstream = await cm.__aenter__()
    except httpx.HTTPError as e:
        logger.warning("passthrough upstream stream error: %s", e)
        # No commit: a connection-level failure means the model never saw the block.
        return Response(
            content=json.dumps(
                {"type": "error", "error": {"type": "api_error", "message": f"upstream error: {e}"}}
            ).encode(),
            status_code=502,
            media_type="application/json",
        )

    # Status is known at stream open, before any body bytes relay — commit here
    # (2xx-gated inside on_status) so a 529 stream open never burns the cadence.
    on_status(upstream.status_code)

    async def relay() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await cm.__aexit__(None, None, None)

    return StreamingResponse(
        relay(),
        status_code=upstream.status_code,
        headers=_response_headers(upstream.headers, decoded_body=False),
        media_type=upstream.headers.get("content-type", "text/event-stream"),
    )
