# pyright: reportPrivateUsage=false
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

import hashlib
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
    _compose_block,  # noqa: F401 — re-exported for callers/tests
    _ComposedBlock,  # noqa: F401 — re-exported for callers/tests
    apply_signal,
    commit_outcome,
)
from agentalloy.api.proxy_context import UpstreamFile, decode_proj_token
from agentalloy.api.proxy_injection import (
    inject_into_anthropic_messages,
    inject_into_anthropic_system_prompt,
)
from agentalloy.api.proxy_models import ProxyMessage, ProxyRequest
from agentalloy.api.proxy_router import (
    _get_phase_telemetry_writer,
    get_embed_client,
    get_orchestrator_for_proxy,
    get_vector_store,
    resolve_passthrough_client,
)
from agentalloy.api.proxy_session import extract_session_header
from agentalloy.api.proxy_signal import SignalResult, evaluate_signal
from agentalloy.api.proxy_telemetry import write_proxy_trace
from agentalloy.providers.base import filter_tools_for_phase

if TYPE_CHECKING:
    from agentalloy.embed_provider import EmbedClient
    from agentalloy.orchestration.compose import ComposeOrchestrator
    from agentalloy.storage.protocols import TelemetryStore
    from agentalloy.telemetry.phase_writer import PhaseTelemetryWriter

logger = logging.getLogger(__name__)

router = APIRouter()

# Re-exported from proxy_apply so existing imports of these symbols from this
# module keep working; the implementations live in the shared seam.
__all__ = ["_ComposedBlock", "_compose_block", "router"]

# Upstream path the discriminator maps to (the /proj/<token> prefix is ours).
_UPSTREAM_PATH = "/v1/messages"

# app.state attribute name for this surface's per-repo passthrough client cache
# (see resolve_passthrough_client) — distinct from the Responses surface's so
# the two never share a cache dict.
_CLIENT_CACHE_ATTR = "anthropic_passthrough_client_cache"

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
    },
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
                content if isinstance(content, str | list) else None,
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


def _flatten_text_field(value: Any) -> str | None:
    """Flatten a system/instructions field (plain string or list of Anthropic-style
    text blocks) to plain text, mirroring ``proxy_router._extract_system_prompt``
    for the chat-completions surface.
    """
    if isinstance(value, str):
        return value or None
    if isinstance(value, list):
        parts = [
            cast("dict[str, Any]", b).get("text", "")
            for b in cast("list[Any]", value)
            if isinstance(b, dict) and cast("dict[str, Any]", b).get("type") == "text"
        ]
        joined = "".join(parts)
        return joined or None
    return None


def _payload_system_prompt_sha(payload: dict[str, Any], field: str) -> str | None:
    """sha256 fingerprint of ``payload[field]`` (Anthropic ``system`` or Responses
    ``instructions``), mirroring ``proxy_router._system_prompt_sha`` so the
    ``workflow_delivered`` telemetry column is comparable across all three proxy
    surfaces.
    """
    text = _flatten_text_field(payload.get(field))
    if not text:
        return None
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _make_on_status(
    project_dir: Path,
    outcome: InjectOutcome[dict[str, Any]] | None,
    vector_store: TelemetryStore | None,
    signal: SignalResult,
    *,
    phase_telemetry: PhaseTelemetryWriter | None = None,
    model: str | None = None,
    system_prompt_sha: str | None = None,
) -> Callable[[int], None]:
    """``on_status`` for the forward: on a 2xx response commit the deferred cadence
    markers (iff a workflow block composed), write one consolidated proxy trace, AND
    write a ``phase_events`` ``llm_sent`` row so instruction-delivery telemetry
    (``workflow_delivered``) exists on this surface too, not just the OpenAI
    chat-completions path (#547 sub-4).

    Best-effort telemetry — the arg-construction is guarded and both writes are
    internally soft-failing, so neither can break the forward. A non-2xx forward
    commits nothing and records nothing (the model never processed the turn).
    """

    def on_status(status: int) -> None:
        ok = 200 <= status < 300
        if outcome is not None:
            commit_outcome(project_dir, outcome, upstream_ok=ok)
        if ok and vector_store is not None:
            try:
                _write_passthrough_trace(vector_store, signal, outcome)
            except Exception:  # noqa: BLE001 — telemetry never breaks the forward
                logger.warning("passthrough telemetry write failed", exc_info=True)
        if ok and phase_telemetry is not None:
            try:
                phase_telemetry.llm_sent(
                    signal.trace_id or "",
                    signal.phase or "unknown",
                    model=model,
                    direction="forward",
                    workflow_skill_id=signal.workflow_skill_id,
                    system_prompt_sha=system_prompt_sha,
                    repo=signal.repo,
                    workflow_delivered=signal.workflow_system_prose is not None,
                )
            except Exception:  # noqa: BLE001 — telemetry never breaks the forward
                logger.debug("llm_sent telemetry write failed", exc_info=True)

    return on_status


def _write_passthrough_trace(
    vector_store: TelemetryStore,
    signal: SignalResult,
    outcome: InjectOutcome[dict[str, Any]] | None,
) -> None:
    """Write one consolidated CompositionTrace for a passthrough forward.

    ``status`` is ``'proxy_composed'`` when the workflow block was injected, else
    ``'proxy_passthrough'`` — a banner-only turn produces no ``outcome`` and does NOT
    count as composed (mirrors ``proxy_router._write_flow_telemetry``, where the
    banner does not flip ``composed``). Every field is sourced from the already
    resolved ``signal`` and ``outcome.telemetry`` (the merged provenance from both
    compose tiers); no value is recomputed here. ``task_prompt`` reuses
    ``signal.task`` (the first-user-message text ``evaluate_signal`` already
    extracted) and ``write_proxy_trace`` truncates it to 500 chars.
    """
    composed = outcome is not None and outcome.injected is not None
    tel = outcome.telemetry if outcome is not None else None
    scores_json = json.dumps(tel.lm_assist_scores) if tel and tel.lm_assist_scores else None
    write_proxy_trace(
        vector_store,
        phase=signal.phase or "unspecified",
        task_prompt=signal.task or "",
        status="proxy_composed" if composed else "proxy_passthrough",
        pre_filter_matched=signal.pre_filter_matched,
        gates_met=signal.gates_met,
        gates_unmet=signal.gates_unmet,
        qwen_calls=signal.qwen_calls,
        # Compose-span latency from the orchestrator's per-leg breakdown (no handler
        # wall-clock is threaded to this surface; this is the measured compose work).
        total_latency_ms=tel.total_latency_ms if tel else None,
        retrieval_latency_ms=tel.retrieval_latency_ms if tel else None,
        source_skill_ids=tel.returned_skill_ids if tel else None,
        system_skill_ids=tel.header_fragment_ids if tel else None,
        workflow_skill_ids=tel.workflow_skill_ids if tel else None,
        selected_fragment_ids=tel.selected_fragment_ids if tel else None,
        tokens_returned=tel.tokens_returned if tel else 0,
        tokens_flat_equivalent=tel.tokens_flat_equivalent if tel else 0,
        reranked=tel.reranked if tel else False,
        lm_assist_outcome=tel.lm_assist_outcome if tel else "disabled",
        lm_assist_model=tel.lm_assist_model if tel else None,
        lm_assist_kept_ids=tel.lm_assist_kept_ids if tel else None,
        lm_assist_dropped_ids=tel.lm_assist_dropped_ids if tel else None,
        lm_assist_scores=scores_json,
        dense_leg_degraded=tel.dense_leg_degraded if tel else False,
        phase_gate_embed_failed=signal.phase_gate_embed_failed,
        repo=signal.repo,
        session_key=signal.session_key,
        session_source=signal.session_source,
        category="paused" if signal.paused_mode else None,
        contract_id=tel.contract_id if tel else None,
        contract_tags=tel.contract_tags if tel else None,
    )


async def _maybe_inject(
    payload: dict[str, Any],
    token: str,
    embed_client: EmbedClient | None,
    orchestrator: ComposeOrchestrator | None,
    session_id: str | None = None,
    *,
    vector_store: TelemetryStore | None = None,
    phase_telemetry: PhaseTelemetryWriter | None = None,
) -> tuple[dict[str, Any] | None, InjectOutcome[dict[str, Any]] | None, SignalResult]:
    """Run signal → compose → inject for this repo.

    Returns ``(payload_or_None, outcome_or_None, signal)``: the new payload (None
    when nothing was injected — skip / no-op), the :class:`InjectOutcome` whose
    cadence markers the caller commits *after a 2xx forward* (None when no workflow
    block was composed), and the resolved :class:`SignalResult` (used to build the
    consolidated telemetry row on the 2xx seam). Raising is fine — the caller treats
    any exception as "forward the original unchanged". ``session_id`` is the harness
    session-id header (Claude Code's ``x-claude-code-session-id``), used to key
    per-session orientation. ``phase_telemetry`` is the app-state-scoped
    ``PhaseTelemetryWriter`` (task 04) threaded through to ``evaluate_signal`` so
    this surface reuses it instead of constructing (and re-migrating the schema
    of) a fresh one per request.
    """
    project_dir = decode_proj_token(token)  # ValueError on a bad token → caller soft-fails
    signal = await evaluate_signal(
        _proxy_request_from_anthropic(payload),
        project_dir,
        embed_client,
        session_id,
        vector_store=vector_store,
        phase_telemetry=phase_telemetry,
    )

    # Three independent injections:
    #   1. the domain/cursor block -> last user message (gated on should_compose),
    #   2. the per-turn phase banner (signal.banner) -> last user message, on EVERY
    #      carrier turn even when no block is composed, and
    #   3. the workflow prose (signal.workflow_system_prose) -> top-level `system`,
    #      also on every carrier turn.
    # The split between 1 and 3 is retrieval-derived vs. not: 3 is phase-pure and so
    # stays byte-identical across a phase (prompt-cache friendly at the front of the
    # request), while 1 varies per turn and rides at the tail where churn is free.
    # The banner injects AFTER the workflow block so it is the freshest text. We track
    # the latest payload across all three and return it iff anything was injected (else
    # None → the caller forwards the original verbatim).
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

        def _inject_anthropic(text: str) -> dict[str, Any] | None:
            # User-message leg only. The workflow prose that used to ride along
            # here now has its own system-prompt leg (step 3) on a per-turn
            # cadence, so this carries just the turn-varying half: advisories,
            # confirms, Tier 1 system fragments, Tier 2 domain, decision push.
            #
            # Still all-or-nothing on a missing user message: with nothing to
            # inject into, this block can never be delivered this turn or any
            # later turn this phase/session (the announced marker would be burned
            # on commit, permanently forfeiting the delivery).
            raw_messages = before.get("messages")
            has_user_message = isinstance(raw_messages, list) and any(
                isinstance(m, dict) and cast(dict[str, Any], m).get("role") == "user"
                for m in cast(list[Any], raw_messages)
            )
            if not has_user_message:
                return None
            injected = inject_into_anthropic_messages(before, text, phase=phase)
            return injected if injected is not before else None

        outcome = await apply_signal(
            signal=signal,
            orchestrator=orchestrator,
            inject=_inject_anthropic,
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
            current,
            signal.banner,
            phase=signal.phase,
            kind="banner",
        )
        if bannered is not current:
            current = bannered

    # 3. Workflow prose -> top-level `system`, on EVERY carrier turn.
    #    The harness rebuilds each request from its own local history and never sees
    #    our mutation, so a once-per-(phase, session) system block survives exactly one
    #    request: the agent read the intake instructions on turn 1 and was guessing by
    #    turn 3. Like the banner, this leg carries no cadence marker and takes no part
    #    in `commit_outcome` — a marker here would recreate the same deliver-once bug.
    #
    #    COST: intended to be ~one cache write per phase change, NOT measured yet.
    #    The prose is phase-pure (see `SignalResult.workflow_system_prose`) so its bytes
    #    are stable across a phase, and `inject_into_anthropic_system_prompt` now makes
    #    the appended block its own `cache_control` breakpoint so that stability is
    #    actually redeemable — otherwise the block sits past the harness's last
    #    breakpoint and is fresh input at 1.0x on every turn.
    #
    #    Two known holes, both of which degrade to "uncached", never to an error:
    #      - a string-valued `system` cannot carry a breakpoint (see that branch),
    #      - the harness may already have spent all 4 breakpoints, in which case we
    #        yield and take the token hit.
    #    Confirm with two same-phase turns on the live proxy: `usage.input_tokens`
    #    should NOT exceed a no-block baseline by the block size, and
    #    `cache_read_input_tokens` should grow to include it.
    if signal.workflow_system_prose and signal.phase is not None:
        sys_injected = inject_into_anthropic_system_prompt(
            current,
            signal.workflow_system_prose,
            phase=signal.phase,
        )
        if sys_injected is not None:
            current = sys_injected

    injected_payload = current if current is not payload else None

    # Filter tools for the current phase (harness-agnostic tool gating).
    # Applied to the final payload so it catches both injected and non-injected paths.
    # When in pause mode, write gating is skipped regardless of phase.
    if injected_payload is not None and signal.phase is not None:
        raw_tools = injected_payload.get("tools")
        if isinstance(raw_tools, list):
            filtered = filter_tools_for_phase(
                cast("list[dict[str, Any]]", raw_tools),
                signal.phase,
                pause_mode=signal.paused_mode,
            )
            if filtered is not raw_tools:
                injected_payload = {**injected_payload, "tools": filtered}

    return injected_payload, outcome, signal


def _response_headers(headers: httpx.Headers, *, decoded_body: bool) -> dict[str, str]:
    """Filter upstream response headers for relay. Drops hop-by-hop, length, and
    (when the body was decoded by httpx) the now-wrong content-encoding. The
    content-type is relayed separately via ``media_type``.
    """
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
    vector_store: TelemetryStore | None = Depends(get_vector_store),
) -> Response | StreamingResponse:
    raw_body = await request.body()
    query_string = request.url.query
    inbound_headers = request.headers

    # Per-repo .agentalloy/upstream (captured by `agentalloy add`) wins over the
    # lifespan-scoped default client -- same precedence as the chat-completions
    # surface's _resolve_upstream, so `add`'s reported upstream is the one this
    # surface actually forwards to (#505's fix, mirrored here for the Anthropic
    # passthrough per the issue's "two surfaces, not one" finding).
    try:
        project_dir = decode_proj_token(token)
    except ValueError:
        project_dir = None
    if project_dir is not None:
        resolved_client = resolve_passthrough_client(
            request.app,
            project_dir,
            client,
            _CLIENT_CACHE_ATTR,
        )
    else:
        resolved_client = client

    # Check for error (UpstreamFile) before the None check
    if isinstance(resolved_client, UpstreamFile) and resolved_client.kind == "error":
        assert resolved_client.detail is not None
        return Response(
            content=json.dumps(
                {
                    "type": "error",
                    "error": {
                        "type": "api_error",
                        "code": "upstream_parse_error",
                        "message": resolved_client.detail,
                    },
                },
            ).encode(),
            status_code=503,
            media_type="application/json",
        )

    if resolved_client is None:
        return Response(
            content=json.dumps(
                {
                    "type": "error",
                    "error": {
                        "type": "api_error",
                        "message": "passthrough upstream not configured",
                    },
                },
            ).encode(),
            status_code=503,
            media_type="application/json",
        )

    assert isinstance(resolved_client, AnthropicPassthroughClient)

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
            phase_telemetry = _get_phase_telemetry_writer(request.app, vector_store)
            injected, outcome, signal = await _maybe_inject(
                payload,
                token,
                embed_client,
                orchestrator,
                session_id,
                vector_store=vector_store,
                phase_telemetry=phase_telemetry,
            )
            if injected is not None:
                body_to_send = json.dumps(injected).encode("utf-8")
            # Set unconditionally on a successful compose: the on_status seam now
            # also writes one consolidated telemetry row, so the passthrough
            # (nothing-composed) case is recorded too — not just the committed-marker
            # case. A compose-path exception leaves on_status = _noop_status (no row;
            # error-path parity deferred).
            final_payload = injected if injected is not None else payload
            model = final_payload.get("model")
            on_status = _make_on_status(
                decode_proj_token(token),
                outcome,
                vector_store,
                signal,
                phase_telemetry=phase_telemetry,
                model=model if isinstance(model, str) else None,
                system_prompt_sha=_payload_system_prompt_sha(final_payload, "system"),
            )
        except Exception:
            logger.warning("passthrough compose/inject failed; forwarding original", exc_info=True)
            body_to_send = raw_body

    # --- Forward. ---
    if stream_flag:
        return await _forward_streaming(
            resolved_client,
            query_string,
            inbound_headers,
            body_to_send,
            on_status,
        )
    return await _forward_once(
        resolved_client,
        query_string,
        inbound_headers,
        body_to_send,
        on_status,
    )


async def _forward_once(
    client: AnthropicPassthroughClient,
    query_string: str,
    inbound_headers: Any,
    body: bytes,
    on_status: Callable[[int], None] = lambda _status: None,
    path: str = _UPSTREAM_PATH,
) -> Response:
    try:
        upstream = await client.forward(
            path=path,
            query_string=query_string,
            inbound_headers=inbound_headers,
            body=body,
        )
    except httpx.HTTPError as e:
        logger.warning("passthrough upstream error: %s", e)
        # No commit: a connection-level failure means the model never saw the block.
        return Response(
            content=json.dumps(
                {
                    "type": "error",
                    "error": {"type": "api_error", "message": f"upstream error: {e}"},
                },
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
    path: str = _UPSTREAM_PATH,
) -> Response | StreamingResponse:
    # Enter the stream manually so we can read the upstream status + headers
    # before constructing the StreamingResponse, then relay raw bytes.
    cm = client.stream(
        path=path,
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
                {
                    "type": "error",
                    "error": {"type": "api_error", "message": f"upstream error: {e}"},
                },
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
