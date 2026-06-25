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
from dataclasses import dataclass
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
from agentalloy.api.proxy_signal import SignalResult, commit_markers, evaluate_signal

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


@dataclass
class _ComposedBlock:
    """Result of :func:`_compose_block`: the text plus per-tier commit signals.

    These report what was *composed*. The caller pairs them with whether the block
    was actually injected (delivery) before committing a marker — composing text the
    request then drops (no user message, malformed content) must NOT burn the marker.

    - ``tier1_text``: the Tier 1 orientation block carried real text — its marker may
      be committed once that text is delivered.
    - ``cursor_terminal``: the Tier 2 domain leg reached a *terminal* state (delivered
      skills OR composed to a clean empty result, NOT a transient compose error). A
      cleanly-empty Tier 2 has nothing to deliver, so its cursor commits even without
      an injection — that is what stops a contract with genuinely no domain skills
      from re-firing every turn.
    - ``cursor_text``: the Tier 2 leg produced non-empty domain text — when True the
      cursor marker additionally requires delivery, so an undelivered domain block
      re-fires next turn instead of being silently lost.
    """

    text: str
    tier1_text: bool
    cursor_terminal: bool
    cursor_text: bool


async def _compose_block(signal: SignalResult, orchestrator: ComposeOrchestrator) -> _ComposedBlock:
    """Compose the prose block to inject.

    Three independent parts, each gated separately:

    - **Eval advisory** — emitted whenever the gate eval produced advisories
      (a transition trigger fired). Light; may recur across turns; carries no marker.
    - **Tier 1 (phase-entry announce)** — the workflow skill's operating prose for
      the phase + its phase-scoped system prose. Emitted once per phase entry
      (``signal.announce``). How to operate here; never carries domain skills.
    - **Tier 2 (per work-item)** — the domain skills for the current work-item
      contract (``signal.current_contract``), keyed off its task, not the phase.
      Emitted once per work-item (``signal.announce_cursor``): phase entry, or an
      ``agentalloy task next``.

    Returns a :class:`_ComposedBlock` whose ``text`` is the parts joined (``""``
    when none has content) and whose flags tell the caller which cadence markers
    are safe to commit post-injection.
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

    # Tier 2: domain skills for the current work-item contract. `tier2_terminal`
    # distinguishes "composed to a clean result" (delivered text OR a legitimate
    # empty — the cursor is done) from "the compose leg threw" (transient — leave
    # the cursor unmarked so it re-fires next turn).
    tier2 = ""
    tier2_terminal = False
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
            tier2_terminal = True
        except Exception:
            logger.warning("Tier 2 domain compose failed -- passing through", exc_info=True)
            tier2 = ""
            tier2_terminal = False

    text = "\n\n".join(p for p in (advisory_block, tier1, tier2) if p)
    return _ComposedBlock(
        text=text,
        tier1_text=bool(tier1),
        cursor_terminal=tier2_terminal,
        cursor_text=bool(tier2),
    )


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

    # Cadence lives in `.agentalloy/{announced,composed}` (durable), not in the
    # request body. The signal layer decided this turn warrants injection but
    # deliberately did NOT commit the markers — we do that here, only after compose
    # tells us what was actually emitted, so a degraded compose (embed down) or an
    # empty block never records the phase/work-item as delivered. The old
    # marker-echo dedup here was structurally dead (Claude Code never persists an
    # injected marker back into the next request) and is gone.
    composed = await _compose_block(signal, orchestrator)
    injected = (
        inject_into_anthropic_messages(payload, composed.text, phase=signal.phase)
        if composed.text
        else None
    )
    # `inject_into_anthropic_messages` returns a NEW dict on a real injection and the
    # SAME `payload` object on every no-op (no user message, already-present marker,
    # malformed/unknown content shape). Identity, not None-ness, is what proves the
    # block actually reached the request — so a turn that composed text but couldn't
    # inject it does NOT burn the marker and re-announces next turn.
    delivered = injected is not None and injected is not payload
    commit_markers(
        project_dir,
        signal,
        # Tier 1: commit only once the orientation text is actually delivered.
        announce_emitted=composed.tier1_text and delivered,
        # Tier 2: a cleanly-empty terminal commits regardless (nothing to deliver);
        # a terminal that produced text commits only once that text is delivered.
        cursor_emitted=composed.cursor_terminal and (delivered or not composed.cursor_text),
    )
    return injected


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
