"""Native Anthropic Messages passthrough (the ``/proj/<token>/v1/messages`` path).

Unlike the ``_anthropic_to_openai`` translation shim at bare ``/v1/messages``,
this path does **no** translation. It:

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
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import httpx
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse

from agentalloy.api.anthropic_passthrough import AnthropicPassthroughClient
from agentalloy.api.compose_models import ComposeRequest, EmptyResult, Phase
from agentalloy.api.proxy_context import decode_proj_token
from agentalloy.api.proxy_injection import inject_into_anthropic_messages
from agentalloy.api.proxy_models import ProxyMessage, ProxyRequest
from agentalloy.api.proxy_router import get_embed_client, get_orchestrator_for_proxy
from agentalloy.api.proxy_session import extract_session_header
from agentalloy.api.proxy_signal import SignalResult, evaluate_signal

if TYPE_CHECKING:
    from agentalloy.embed_provider import EmbedClient
    from agentalloy.orchestration.compose import ComposeOrchestrator

logger = logging.getLogger(__name__)

router = APIRouter()

_VALID_PHASES = ("intake", "spec", "design", "build", "qa", "ship", "sdd-fast")

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

    The signal layer only reads user-message text (to derive the task prompt);
    Anthropic message content (str or a list of content blocks) maps straight
    onto ``ProxyMessage.content``. The top-level Anthropic ``system`` field is
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
    return ProxyRequest(model=model if isinstance(model, str) else "unknown", messages=messages)


async def _compose_block(signal: SignalResult, orchestrator: ComposeOrchestrator) -> str:
    """Compose the prose block to inject, or "".

    Three independent parts, each gated separately:

    - **Eval advisory** — emitted whenever the gate eval produced advisories
      (a transition trigger fired). Light; may recur across turns.
    - **Tier 1 (phase-entry announce)** — the workflow skill's operating prose for
      the phase + its phase-scoped system prose. Emitted once per phase entry
      (``signal.announce``). How to operate here; never carries domain skills.
    - **Tier 2 (per work-item)** — the domain skills for the current work-item
      contract (``signal.current_contract``), keyed off its task, not the phase.
      Emitted once per work-item (``signal.announce_cursor``): phase entry, or an
      ``agentalloy task next``.

    Returns the parts joined, or "" when none has content.
    """
    phase = signal.phase
    compose_phase: Phase = phase if phase in _VALID_PHASES else "build"  # type: ignore[assignment]

    advisory_block = ""
    if signal.advisories:
        advisory_block = (
            "[agentalloy-eval]\n" + "\n".join(signal.advisories) + "\n[/agentalloy-eval]"
        )

    # Tier 1: workflow prose (operating instructions) + system-only compose.
    tier1 = ""
    if signal.announce:
        parts: list[str] = []
        if signal.workflow_prose:
            parts.append(signal.workflow_prose.strip())
        try:
            system_req = ComposeRequest(
                task=signal.task or f"Entering {compose_phase}.",
                phase=compose_phase,
                legs="system",
            )
            result = await orchestrator.compose(
                system_req,
                repo=signal.repo,
                session_key=signal.session_key,
                session_source=signal.session_source,
            )
            if not isinstance(result, EmptyResult) and result.output:
                parts.append(result.output)
        except Exception:
            logger.warning("Tier 1 system compose failed -- workflow prose only", exc_info=True)
        tier1 = "\n\n".join(parts)

    # Tier 2: domain skills for the current work-item contract.
    tier2 = ""
    if signal.announce_cursor and signal.current_contract:
        try:
            from agentalloy.api.compose_models import compose_request_from_contract
            from agentalloy.contracts import parse_contract

            contract = parse_contract(Path(signal.current_contract))
            domain_req = compose_request_from_contract(contract, legs="domain")
            result = await orchestrator.compose(
                domain_req,
                repo=signal.repo,
                session_key=signal.session_key,
                session_source=signal.session_source,
            )
            tier2 = "" if isinstance(result, EmptyResult) else result.output
        except Exception:
            logger.warning("Tier 2 domain compose failed -- passing through", exc_info=True)
            tier2 = ""

    return "\n\n".join(p for p in (advisory_block, tier1, tier2) if p)


async def _maybe_inject(
    payload: dict[str, Any],
    token: str,
    embed_client: EmbedClient | None,
    orchestrator: ComposeOrchestrator | None,
    session_id: str | None = None,
) -> dict[str, Any] | None:
    """Run signal → compose → inject for this repo. Return a new payload, or None.

    Returns None when nothing was injected (skip / no-op). Raising is fine — the
    caller treats any exception as "forward the original unchanged". ``session_id``
    is the harness session-id header (Claude Code's ``x-claude-code-session-id``),
    used to key per-session orientation.
    """
    project_dir = decode_proj_token(token)  # ValueError on a bad token → caller soft-fails
    signal = await evaluate_signal(
        _proxy_request_from_anthropic(payload), project_dir, embed_client, session_id
    )
    if not (signal.should_compose and signal.phase and orchestrator is not None):
        return None

    # Cadence lives in `.agentalloy/announced` (durable, owned by the signal
    # layer), not in the request body. The old marker-echo dedup here was
    # structurally dead — Claude Code never persists an injected marker back into
    # the next request, so it never matched — and is gone. The signal layer has
    # already decided this turn warrants injection (entry announce and/or eval
    # advisory); we just compose and inject.
    block = await _compose_block(signal, orchestrator)
    if not block:
        return None
    return inject_into_anthropic_messages(payload, block, phase=signal.phase)


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

    if payload is not None:
        try:
            session_id = extract_session_header(inbound_headers)
            injected = await _maybe_inject(payload, token, embed_client, orchestrator, session_id)
            if injected is not None:
                body_to_send = json.dumps(injected).encode("utf-8")
        except Exception:
            logger.warning("passthrough compose/inject failed; forwarding original", exc_info=True)
            body_to_send = raw_body

    # --- Forward. ---
    if stream_flag:
        return await _forward_streaming(client, query_string, inbound_headers, body_to_send)
    return await _forward_once(client, query_string, inbound_headers, body_to_send)


async def _forward_once(
    client: AnthropicPassthroughClient,
    query_string: str,
    inbound_headers: Any,
    body: bytes,
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
        return Response(
            content=json.dumps(
                {"type": "error", "error": {"type": "api_error", "message": f"upstream error: {e}"}}
            ).encode(),
            status_code=502,
            media_type="application/json",
        )
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
        return Response(
            content=json.dumps(
                {"type": "error", "error": {"type": "api_error", "message": f"upstream error: {e}"}}
            ).encode(),
            status_code=502,
            media_type="application/json",
        )

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
