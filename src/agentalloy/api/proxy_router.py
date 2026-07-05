"""Proxy router — forwards chat completions to the upstream LLM.

Full integrated handler:
  parse -> resolve cwd -> signal layer -> compose+inject -> forward -> telemetry

Handles both non-streaming (JSON) and streaming (SSE) responses.
Composition failures soft-fail: request passes through unchanged.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from agentalloy.api.proxy_apply import (
    InjectOutcome,
    ProxyComposeTelemetry,
    apply_signal,
    commit_outcome,
)
from agentalloy.api.proxy_context import (
    decode_proj_token,
    read_phase,
    read_upstream,
    resolve_working_dir,
)
from agentalloy.api.proxy_injection import inject_into_openai_messages
from agentalloy.api.proxy_models import ProxyRequest
from agentalloy.api.proxy_session import extract_session_header, resolve_session_key
from agentalloy.api.proxy_signal import evaluate_signal
from agentalloy.api.proxy_telemetry import write_proxy_trace
from agentalloy.api.upstream.error_sse import error_sse_plain

if TYPE_CHECKING:
    from agentalloy.config import Settings as AppSettings
    from agentalloy.embed_provider import EmbedClient
    from agentalloy.orchestration.compose import ComposeOrchestrator
    from agentalloy.storage.protocols import TelemetryStore

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


def get_vector_store(request: Request) -> TelemetryStore | None:
    """Return the telemetry store from app.state (the proxy trace sink).

    Named ``get_vector_store`` for call-site stability; in v5 the proxy telemetry
    path writes composition traces to the service-owned telemetry.duck, so this
    resolves ``app.state.telemetry_store`` (not the Lance fragment store).
    """
    return getattr(request.app.state, "telemetry_store", None)


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
# Upstream resolution (per-repo .agentalloy/upstream → global fallback)
# ---------------------------------------------------------------------------


def _get_or_create_upstream_client(
    app: Any, base_url: str, api_key: str | None
) -> httpx.AsyncClient:
    """Return a cached httpx client for *base_url* (per-repo upstream).

    Cached on ``app.state.upstream_client_cache`` keyed by ``base_url`` so each
    distinct captured upstream reuses one connection pool. The client carries no
    ``base_url`` of its own — callers post absolute URLs — so a harness upstream
    served under a subpath (``…/v1``) is preserved verbatim rather than mangled
    by httpx base-path joining. Closed on lifespan shutdown.
    """
    cache: dict[str, httpx.AsyncClient] | None = getattr(app.state, "upstream_client_cache", None)
    if cache is None:
        cache = {}
        app.state.upstream_client_cache = cache
    client = cache.get(base_url)
    if client is None:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        client = httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(connect=5.0, read=300.0, write=30.0, pool=5.0),
        )
        cache[base_url] = client
    return client


def _resolve_upstream(
    app: Any,
    cwd: Path,
    default_client: httpx.AsyncClient | None,
    default_model: str,
) -> tuple[httpx.AsyncClient, str, str] | None:
    """Resolve ``(client, chat_completions_url, model)`` for a request.

    A per-repo ``.agentalloy/upstream`` (captured by ``agentalloy add``) wins:
    the proxy adopts the harness's own upstream, forwarding to
    ``<url>/chat/completions`` with the API key read from the named env var at
    request time. Otherwise falls back to the global lifespan-scoped client
    (``default_client``, posting the relative ``/v1/chat/completions``). Returns
    ``None`` only when neither resolves — the caller then 503s.
    """
    up = read_upstream(cwd)
    if up is not None:
        api_key = os.environ.get(up.key_env) if up.key_env else None
        client = _get_or_create_upstream_client(app, up.url, api_key)
        return client, f"{up.url}/chat/completions", up.model
    if default_client is not None:
        return default_client, "/v1/chat/completions", default_model
    return None


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
    upstream: httpx.AsyncClient,
    chat_url: str,
    payload: dict[str, Any],
    on_status: Callable[[int], None] = lambda _status: None,
) -> StreamingResponse:
    """Forward a streaming (SSE) response from the upstream LLM.

    ``chat_url`` is the chat-completions endpoint — relative (``/v1/chat/...``)
    for the global client, absolute (``<captured>/chat/completions``) for a
    per-repo adopted upstream.

    ``on_status`` is invoked once with the upstream status as soon as the stream
    opens (before any chunk relays), so the caller can commit cadence markers
    2xx-gated — a 5xx open never burns the cadence.
    """

    async def event_generator() -> AsyncGenerator[str, None]:
        try:
            async with upstream.stream("POST", chat_url, json=payload) as resp:
                on_status(resp.status_code)
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
        # exclude_none: strict upstreams (llama.cpp) reject explicit nulls on
        # optional message fields — e.g. `"tool_call_id": null` fails template
        # parsing with "type must be string, but is null".
        "messages": [m.model_dump(exclude_none=True) for m in request.messages],
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
        # "cwd" is the proxy's own repo-resolution channel (resolve_working_dir),
        # not an upstream concern — strict OpenAI-compat servers can reject
        # unknown metadata keys. Forward the rest (e.g. qwen-oauth sessionId).
        upstream_metadata = {k: v for k, v in request.metadata.items() if k != "cwd"}
        if upstream_metadata:
            payload["metadata"] = upstream_metadata
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
    vector_store: TelemetryStore | None,
    request: ProxyRequest,
    phase: str | None,
    composed: bool,
    pre_filter_matched: str | None,
    gates_met: list[str] | None,
    gates_unmet: list[str] | None,
    qwen_calls: int,
    latency_ms: int | None,
    error_code: str | None = None,
    telemetry: ProxyComposeTelemetry | None = None,
    phase_gate_embed_failed: bool = False,
    repo: str | None = None,
    session_key: str | None = None,
    session_source: str | None = None,
    category: str | None = None,
) -> None:
    """Write one consolidated telemetry trace for the full proxy request flow.

    ``telemetry`` carries the merged skill/fragment provenance from both compose
    tiers (the orchestrator's per-leg writes are suppressed via
    ``record_trace=False``); ``None`` on passthrough/error paths leaves the skill
    fields empty. ``repo`` and ``session_*`` are the values the handler already
    resolved (the handler may have used a /proj/<token> override, so they're passed
    in rather than recomputed here).
    """
    if vector_store is None:
        return
    status = "proxy_composed" if composed else "proxy_passthrough"
    task_prompt = _extract_task_prompt(request)
    scores_json = (
        json.dumps(telemetry.lm_assist_scores) if telemetry and telemetry.lm_assist_scores else None
    )
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
        retrieval_latency_ms=telemetry.retrieval_latency_ms if telemetry else None,
        source_skill_ids=telemetry.returned_skill_ids if telemetry else None,
        system_skill_ids=telemetry.header_fragment_ids if telemetry else None,
        workflow_skill_ids=telemetry.workflow_skill_ids if telemetry else None,
        selected_fragment_ids=telemetry.selected_fragment_ids if telemetry else None,
        tokens_returned=telemetry.tokens_returned if telemetry else 0,
        tokens_flat_equivalent=telemetry.tokens_flat_equivalent if telemetry else 0,
        reranked=telemetry.reranked if telemetry else False,
        lm_assist_outcome=telemetry.lm_assist_outcome if telemetry else "disabled",
        lm_assist_model=telemetry.lm_assist_model if telemetry else None,
        lm_assist_kept_ids=telemetry.lm_assist_kept_ids if telemetry else None,
        lm_assist_dropped_ids=telemetry.lm_assist_dropped_ids if telemetry else None,
        lm_assist_scores=scores_json,
        dense_leg_degraded=telemetry.dense_leg_degraded if telemetry else False,
        error_code=error_code,
        session_key=session_key,
        session_source=session_source,
        phase_gate_embed_failed=phase_gate_embed_failed,
        repo=repo,
        category=category,
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
    vector_store: TelemetryStore | None = Depends(get_vector_store),
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

    # Resolve the upstream to forward to: a per-repo .agentalloy/upstream (adopted
    # from the harness's own config by `agentalloy add`) wins, else the global
    # lifespan client. 503 only when neither resolves.
    resolved_upstream = _resolve_upstream(
        fastapi_request.app, cwd, upstream, settings.upstream_model
    )
    if resolved_upstream is None:
        return _upstream_not_configured_error()
    upstream_client, chat_url, upstream_model = resolved_upstream

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
    # Same `evaluate_signal → compose → inject → commit_markers` cycle as the
    # Anthropic passthrough, via the shared `apply_signal` seam. Injection lands in
    # the LAST user message (the system block stays byte-identical for prompt-cache
    # safety); markers are committed only after a confirmed, non-empty injection, so
    # a degraded compose never burns the announce/cursor cadence.
    # `current` tracks the latest request across two independent injections (workflow
    # block, then the per-turn banner). `composed` flips ONLY for the workflow block —
    # the banner is a recency anchor and must not register as a composition in telemetry.
    current = request
    modified_request = request
    compose_telemetry: ProxyComposeTelemetry | None = None
    # Deferred cadence commit: apply_signal no longer writes `.agentalloy/{announced,
    # composed}` — we commit only after a confirmed 2xx upstream response (see
    # `_commit` below), so a turn the model never processed (5xx/connection error)
    # re-announces on the harness retry instead of silently dropping orientation.
    inject_outcome: InjectOutcome[ProxyRequest] | None = None
    if (
        signal_result is not None
        and signal_result.should_compose
        and signal_result.phase
        and orchestrator is not None
    ):
        phase = signal_result.phase
        try:
            before = current

            def _inject_openai(text: str) -> ProxyRequest | None:
                new_msgs = inject_into_openai_messages(before.messages, text, phase=phase)
                return (
                    before.model_copy(update={"messages": new_msgs})
                    if new_msgs is not None
                    else None
                )

            inject_outcome = await apply_signal(
                signal=signal_result,
                orchestrator=orchestrator,
                inject=_inject_openai,
                # The OpenAI injector returns None on every no-op, so a non-None
                # result IS the delivery proof — no identity test needed here.
                delivered=lambda _out: True,
            )
            compose_telemetry = inject_outcome.telemetry
            if inject_outcome.injected is not None:
                current = inject_outcome.injected
                composed = True
        except Exception:
            logger.warning(
                "Composition/injection failed -- passing through unchanged", exc_info=True
            )
            current = request

    # Per-turn banner — appended LAST so it is the freshest text. Runs even when
    # should_compose is False (a banner-only turn), so it sits OUTSIDE the compose
    # guard. Carrier-gated upstream: evaluate_signal only sets `banner` on a carrier
    # turn. The banner must NOT flip `composed` (telemetry tracks composition, not the
    # recency anchor). Soft: any failure leaves `current` unchanged.
    if (
        signal_result is not None
        and signal_result.banner is not None
        and signal_result.phase is not None
    ):
        try:
            new_msgs = inject_into_openai_messages(
                current.messages, signal_result.banner, phase=signal_result.phase, kind="banner"
            )
            if new_msgs is not None:
                current = current.model_copy(update={"messages": new_msgs})
        except Exception:
            logger.warning("Banner injection failed -- skipping banner", exc_info=True)

    modified_request = current

    # Carry the phase-gate embed-failure flag into every telemetry write below
    # (computed once; the value is the same for all exit paths of this request).
    gate_embed_failed = signal_result.phase_gate_embed_failed if signal_result else False
    # Mode tag for every telemetry write of this request: "free-flow" rows are
    # distinguishable so free→contract conversion is measurable later.
    trace_category = "free-flow" if signal_result and signal_result.free_mode else None

    def _commit(status: int) -> None:
        """Commit the deferred cadence markers, 2xx-gated. No-op if nothing composed."""
        if inject_outcome is not None:
            commit_outcome(cwd, inject_outcome, upstream_ok=200 <= status < 300)

    # --- Step 5: Forward to upstream ---
    try:
        payload = _build_payload(modified_request, upstream_model)
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
            telemetry=compose_telemetry,
            phase_gate_embed_failed=gate_embed_failed,
            repo=repo,
            session_key=session_key,
            session_source=session_source,
            category=trace_category,
        )
        return _stream_upstream_response(upstream_client, chat_url, payload, on_status=_commit)

    # Non-streaming: forward and return JSON
    try:
        resp = await upstream_client.post(chat_url, json=payload)
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
            telemetry=compose_telemetry,
            phase_gate_embed_failed=gate_embed_failed,
            repo=repo,
            session_key=session_key,
            session_source=session_source,
            category=trace_category,
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
            telemetry=compose_telemetry,
            phase_gate_embed_failed=gate_embed_failed,
            repo=repo,
            session_key=session_key,
            session_source=session_source,
            category=trace_category,
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
            telemetry=compose_telemetry,
            phase_gate_embed_failed=gate_embed_failed,
            repo=repo,
            session_key=session_key,
            session_source=session_source,
            category=trace_category,
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
            telemetry=compose_telemetry,
            phase_gate_embed_failed=gate_embed_failed,
            repo=repo,
            session_key=session_key,
            session_source=session_source,
            category=trace_category,
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
            telemetry=compose_telemetry,
            phase_gate_embed_failed=gate_embed_failed,
            repo=repo,
            session_key=session_key,
            session_source=session_source,
            category=trace_category,
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
            telemetry=compose_telemetry,
            phase_gate_embed_failed=gate_embed_failed,
            repo=repo,
            session_key=session_key,
            session_source=session_source,
            category=trace_category,
        )
        # Raw passthrough: Response does not re-encode, so a non-JSON upstream
        # body is forwarded verbatim with its original Content-Type (JSONResponse
        # would json.dumps() the text, double-encoding it).
        _commit(resp.status_code)
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
        telemetry=compose_telemetry,
        phase_gate_embed_failed=gate_embed_failed,
        repo=repo,
        session_key=session_key,
        session_source=session_source,
        category=trace_category,
    )

    _commit(resp.status_code)
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
