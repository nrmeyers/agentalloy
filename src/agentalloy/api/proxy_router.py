"""Proxy router — forwards chat completions to the upstream LLM.

Full integrated handler:
  parse -> resolve cwd -> signal layer -> compose+inject -> forward -> telemetry

Handles both non-streaming (JSON) and streaming (SSE) responses.
Composition failures soft-fail: request passes through unchanged.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from agentalloy.api.anthropic_passthrough import AnthropicPassthroughClient
from agentalloy.api.proxy_apply import (
    InjectOutcome,
    ProxyComposeTelemetry,
    apply_signal,
    commit_outcome,
)
from agentalloy.api.proxy_context import (
    UpstreamFile,
    decode_proj_token,
    read_phase,
    read_upstream,
    resolve_working_dir,
)
from agentalloy.api.proxy_injection import (
    inject_into_openai_messages,
    inject_into_openai_system_prompt,
)
from agentalloy.api.proxy_models import ProxyRequest
from agentalloy.api.proxy_session import extract_session_header, resolve_session_key
from agentalloy.api.proxy_signal import evaluate_signal
from agentalloy.api.proxy_telemetry import write_proxy_trace
from agentalloy.api.upstream.error_sse import error_sse_plain
from agentalloy.providers.base import filter_tools_for_phase
from agentalloy.telemetry.phase_writer import PhaseTelemetryWriter

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


def _get_phase_telemetry_writer(
    app: Any, vector_store: TelemetryStore | None
) -> PhaseTelemetryWriter | None:
    """Prefer the lifespan-scoped writer on ``app.state.phase_telemetry`` (task
    04) so the schema DDL is not re-run on every single write. Falls back to a
    fresh per-request writer when app.state has none wired (e.g. tests that
    build the app without the default lifespan) — same soft-fail posture as
    every other telemetry seam in this module.
    """
    # Not an isinstance check: tests wire a MagicMock/duck-typed stand-in onto
    # app.state.phase_telemetry to assert on llm_sent/llm_received/llm_error
    # without a real DuckDB — any truthy value with the writer's method names
    # is accepted, same as every other app.state dependency in this module.
    existing = getattr(app.state, "phase_telemetry", None)
    if existing is not None:
        return existing
    if vector_store is None:
        return None
    return PhaseTelemetryWriter(vector_store)


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
) -> tuple[httpx.AsyncClient, str, str] | UpstreamFile | None:
    """Resolve ``(client, chat_completions_url, model)`` for a request.

    A per-repo ``.agentalloy/upstream`` (captured by ``agentalloy add``) wins:
    the proxy adopts the harness's own upstream, forwarding to
    ``<url>/chat/completions`` with the API key read from the named env var at
    request time. Otherwise falls back to the global lifespan-scoped client
    (``default_client``, posting the relative ``/v1/chat/completions``).
    Returns ``UpstreamFile(kind="error")`` when the per-repo file is
    malformed, ``None`` when neither resolves — the caller then 503s.
    """
    result = read_upstream(cwd)
    if result.kind == "valid" and result.upstream is not None:
        api_key = os.environ.get(result.upstream.key_env) if result.upstream.key_env else None
        client = _get_or_create_upstream_client(app, result.upstream.url, api_key)
        return client, f"{result.upstream.url}/chat/completions", result.upstream.model
    if result.kind == "error":
        return result
    # "absent" or "valid" without upstream — fall through to global
    if default_client is not None:
        return default_client, "/v1/chat/completions", default_model
    return None


def _passthrough_base_url(url: str) -> str:
    """Strip a trailing ``/v1`` from a captured upstream URL for the passthrough
    surfaces.

    ``.agentalloy/upstream`` is documented (and written by ``agentalloy add``)
    with a ``/v1`` suffix — the chat-completions shape, e.g.
    ``http://host:8080/v1``. The passthrough surfaces' ``_UPSTREAM_PATH``
    already carries the versioned path (``/v1/messages`` or ``/v1/responses``),
    so verbatim reuse of the captured URL as the passthrough base would double
    it to ``/v1/v1/messages``. Only a trailing ``/v1`` is stripped — any other
    shape is passed through unchanged.
    """
    return url.removesuffix("/v1")


def resolve_passthrough_client(
    app: Any,
    cwd: Path,
    default_client: AnthropicPassthroughClient | None,
    cache_attr: str,
) -> AnthropicPassthroughClient | None:
    """Resolve the ``AnthropicPassthroughClient`` for this request (Anthropic
    Messages and OpenAI Responses passthrough surfaces).

    Mirrors ``_resolve_upstream``'s per-repo-wins precedence but returns a
    cached client rather than an ``(httpx.AsyncClient, url, model)`` tuple —
    the two client shapes differ materially (relative-URL posting with a
    pre-baked bearer vs. verbatim path-forwarding with the caller's own
    headers), so this is kept as its own small function rather than unified
    with ``_resolve_upstream``.

    A per-repo ``.agentalloy/upstream`` (captured by ``agentalloy add``) wins:
    the proxy adopts the harness's own upstream as the passthrough base URL
    (its ``/v1`` suffix stripped — see ``_passthrough_base_url``). Otherwise
    falls back to ``default_client`` (the lifespan-scoped client built from
    global settings). Returns ``None`` only when neither resolves.

    ``Upstream.key_env`` plays NO role here: the passthrough surfaces are
    auth-transparent by design, forwarding the caller's own ``authorization``/
    ``x-api-key`` header verbatim. A per-repo override changes only the
    destination and must never inject a credential of its own.

    ``cache_attr`` is the ``app.state`` attribute name the per-base-url client
    cache lives on (e.g. ``"anthropic_passthrough_client_cache"``) — kept
    distinct per surface so the Anthropic and Responses passthroughs never
    share a cache dict.
    """
    result = read_upstream(cwd)
    if result.kind == "valid" and result.upstream is not None:
        base_url = _passthrough_base_url(result.upstream.url)
        cache: dict[str, AnthropicPassthroughClient] | None = getattr(app.state, cache_attr, None)
        if cache is None:
            cache = {}
            setattr(app.state, cache_attr, cache)
        client = cache.get(base_url)
        if client is None:
            client = AnthropicPassthroughClient(upstream_base_url=base_url)
            cache[base_url] = client
        return client
    return default_client


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


def _upstream_parse_error(detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "error": {
                "code": "upstream_parse_error",
                "message": f"Per-repo upstream is malformed: {detail}",
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
# LLM forwarding telemetry (llm_sent / llm_received / llm_error)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _StreamTelemetry:
    """Bundle threaded into the SSE generator so it can emit the terminal
    llm_received/llm_error event itself (the handler has already returned by
    the time the stream finishes)."""

    writer: PhaseTelemetryWriter
    trace_id: str | None
    phase: str | None
    model: str | None
    repo: str | None = None


def _extract_system_prompt(request: ProxyRequest) -> str | None:
    """Flatten the first system message to plain text for the sha256 fingerprint."""
    for msg in request.messages:
        if msg.role != "system" or not msg.content:
            continue
        if isinstance(msg.content, str):
            return msg.content
        parts = [block.get("text", "") for block in msg.content if block.get("type") == "text"]
        joined = "".join(parts)
        if joined:
            return joined
    return None


def _system_prompt_sha(request: ProxyRequest) -> str | None:
    text = _extract_system_prompt(request)
    if not text:
        return None
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _extract_tokens_out(body: dict[str, Any]) -> int | None:
    """Pull ``usage.completion_tokens`` from a non-streaming chat-completions body."""
    usage = body.get("usage")
    if isinstance(usage, dict):
        val = cast(dict[str, Any], usage).get("completion_tokens")
        if isinstance(val, int):
            return val
    return None


class _SseUsageScanner:
    """Scan SSE text chunks for a terminal ``usage.completion_tokens`` field.

    Only present on OpenAI chat-completions streams when the caller set
    ``stream_options.include_usage`` — absent otherwise, in which case
    ``latest`` stays None rather than inventing a token count.

    ``resp.aiter_text()`` yields byte-boundary chunks, not line-aligned ones —
    a ``data: {...}`` line carrying the usage block can straddle two chunks, so
    a per-chunk ``json.loads`` (no cross-chunk buffer) silently misses it on a
    fragmented stream. This carries the trailing partial line across ``feed()``
    calls so a split line is only parsed once it's whole.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self.latest: int | None = None

    def feed(self, chunk: str) -> None:
        self._buffer += chunk
        # Keep whatever comes after the last newline — it may be a partial
        # line that the next chunk completes.
        *complete_lines, self._buffer = self._buffer.split("\n")
        for raw_line in complete_lines:
            self._scan_line(raw_line.strip())

    def _scan_line(self, line: str) -> None:
        if not line.startswith("data:"):
            return
        data = line[len("data:") :].strip()
        if not data or data == "[DONE]":
            return
        try:
            obj = json.loads(data)
        except ValueError:
            return
        usage = cast(dict[str, Any], obj).get("usage") if isinstance(obj, dict) else None
        if isinstance(usage, dict):
            val = cast(dict[str, Any], usage).get("completion_tokens")
            if isinstance(val, int):
                self.latest = val


def _emit_llm_sent(
    writer: PhaseTelemetryWriter | None,
    trace_id: str | None,
    phase: str | None,
    model: str | None,
    *,
    workflow_skill_id: str | None,
    system_prompt_sha: str | None,
    repo: str | None = None,
    workflow_delivered: bool | None = None,
) -> None:
    if writer is None:
        return
    try:
        writer.llm_sent(
            trace_id or "",
            phase or "unknown",
            model=model,
            direction="forward",
            workflow_skill_id=workflow_skill_id,
            system_prompt_sha=system_prompt_sha,
            repo=repo,
            workflow_delivered=workflow_delivered,
        )
    except Exception:  # noqa: BLE001 — soft-fail by design
        logger.debug("llm_sent telemetry write failed", exc_info=True)


def _emit_llm_received(
    writer: PhaseTelemetryWriter | None,
    trace_id: str | None,
    phase: str | None,
    model: str | None,
    *,
    tokens_out: int | None,
    latency_ms: int,
    repo: str | None = None,
) -> None:
    if writer is None:
        return
    try:
        writer.llm_received(
            trace_id or "",
            phase or "unknown",
            model=model,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            success=True,
            repo=repo,
        )
    except Exception:  # noqa: BLE001 — soft-fail by design
        logger.debug("llm_received telemetry write failed", exc_info=True)


def _emit_llm_error(
    writer: PhaseTelemetryWriter | None,
    trace_id: str | None,
    phase: str | None,
    model: str | None,
    *,
    error_message: str,
    latency_ms: int,
    repo: str | None = None,
) -> None:
    if writer is None:
        return
    try:
        writer.llm_error(
            trace_id or "",
            phase or "unknown",
            model=model,
            error_message=error_message,
            latency_ms=latency_ms,
            success=False,
            repo=repo,
        )
    except Exception:  # noqa: BLE001 — soft-fail by design
        logger.debug("llm_error telemetry write failed", exc_info=True)


# ---------------------------------------------------------------------------
# Streaming helper
# ---------------------------------------------------------------------------


def _stream_upstream_response(
    upstream: httpx.AsyncClient,
    chat_url: str,
    payload: dict[str, Any],
    on_status: Callable[[int], None] = lambda _status: None,
    telemetry: _StreamTelemetry | None = None,
) -> StreamingResponse:
    """Forward a streaming (SSE) response from the upstream LLM.

    ``chat_url`` is the chat-completions endpoint — relative (``/v1/chat/...``)
    for the global client, absolute (``<captured>/chat/completions``) for a
    per-repo adopted upstream.

    ``on_status`` is invoked once with the upstream status as soon as the stream
    opens (before any chunk relays), so the caller can commit cadence markers
    2xx-gated — a 5xx open never burns the cadence.

    ``telemetry``, when set, drives exactly one terminal ``llm_received`` (normal
    completion) or ``llm_error`` (any failure, including mid-stream after bytes
    were already relayed) emission from inside the generator — the handler has
    already returned a ``StreamingResponse`` by the time the stream finishes, so
    this is the only place that can observe the terminal outcome. ``llm_sent``
    is emitted by the caller before this function is invoked.
    """

    async def event_generator() -> AsyncGenerator[str, None]:
        dispatch_start = time.monotonic()
        usage_scanner = _SseUsageScanner()
        emitted = False

        def _finish_received() -> None:
            nonlocal emitted
            if emitted or telemetry is None:
                return
            emitted = True
            _emit_llm_received(
                telemetry.writer,
                telemetry.trace_id,
                telemetry.phase,
                telemetry.model,
                tokens_out=usage_scanner.latest,
                latency_ms=int((time.monotonic() - dispatch_start) * 1000),
                repo=telemetry.repo,
            )

        def _finish_error(message: str) -> None:
            nonlocal emitted
            if emitted or telemetry is None:
                return
            emitted = True
            _emit_llm_error(
                telemetry.writer,
                telemetry.trace_id,
                telemetry.phase,
                telemetry.model,
                error_message=message,
                latency_ms=int((time.monotonic() - dispatch_start) * 1000),
                repo=telemetry.repo,
            )

        try:
            async with upstream.stream("POST", chat_url, json=payload) as resp:
                on_status(resp.status_code)
                if resp.status_code >= 500:
                    logger.warning("Upstream streaming returned HTTP %d", resp.status_code)
                    yield error_sse_plain(
                        f"Upstream returned HTTP {resp.status_code}", resp.status_code
                    )
                    _finish_error(f"Upstream returned HTTP {resp.status_code}")
                    return
                async for chunk in resp.aiter_text():
                    usage_scanner.feed(chunk)
                    yield chunk
            _finish_received()
        except httpx.HTTPStatusError as exc:
            logger.warning("Upstream streaming HTTP status error: %s", exc)
            yield error_sse_plain(f"Upstream HTTP error: {exc}", exc.response.status_code)
            _finish_error(f"Upstream HTTP error: {exc}")
        except httpx.ConnectError as exc:
            logger.warning("Upstream streaming connection failed: %s", exc)
            yield error_sse_plain(f"Upstream connection failed: {exc}")
            _finish_error(f"Upstream connection failed: {exc}")
        except httpx.TimeoutException as exc:
            logger.warning("Upstream streaming timed out: %s", exc)
            yield error_sse_plain(f"Upstream timeout: {exc}")
            _finish_error(f"Upstream timeout: {exc}")
        except httpx.RequestError as exc:
            logger.warning("Upstream streaming request error: %s", exc)
            yield error_sse_plain(f"Upstream request error: {exc}")
            _finish_error(f"Upstream request error: {exc}")
        except httpx.HTTPError as exc:
            logger.warning("Upstream streaming HTTP error: %s", exc)
            yield error_sse_plain(f"Upstream HTTP error: {exc}")
            _finish_error(f"Upstream HTTP error: {exc}")
        except Exception as exc:
            logger.warning("Upstream streaming unexpected error: %s", exc, exc_info=True)
            yield error_sse_plain(f"Upstream error: {exc}")
            _finish_error(f"Upstream error: {exc}")

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


def _build_payload(
    request: ProxyRequest,
    upstream_model: str | None = None,
    phase: str | None = None,
    *,
    free_mode: bool = False,
) -> dict[str, Any]:
    """Build the JSON payload to forward to the upstream LLM.

    If *upstream_model* is set, overrides ``request.model`` so that synthetic
    model names (e.g. "agentalloy-proxy" from Continue) are mapped to the
    actual upstream model.

    If *phase* is set, tools are filtered through ``filter_tools_for_phase``
    so code-writing tools are removed during denied phases.  When *free_mode*
    is ``True``, write gating is skipped regardless of phase.

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
        tools = request.tools
        if phase is not None:
            tools = filter_tools_for_phase(tools, phase, free_mode=free_mode)
        payload["tools"] = tools
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
    trace_id: str | None = None,
    phase_telemetry: PhaseTelemetryWriter | None = None,
) -> None:
    """Write one consolidated telemetry trace for the full proxy request flow.

    ``telemetry`` carries the merged skill/fragment provenance from both compose
    tiers (the orchestrator's per-leg writes are suppressed via
    ``record_trace=False``); ``None`` on passthrough/error paths leaves the skill
    fields empty. ``repo`` and ``session_*`` are the values the handler already
    resolved (the handler may have used a /proj/<token> override, so they're passed
    in rather than recomputed here). ``phase_telemetry``, when supplied, is the
    app-state-scoped writer (task 04) — reused instead of constructing a fresh
    one so the schema DDL doesn't re-run on every write; falls back to a
    per-call writer over ``vector_store`` when not supplied (e.g. call sites
    that don't have app.state, such as tests).
    """
    if vector_store is None:
        return
    # Phase-level telemetry: complete or error, using the request's trace_id.
    try:
        pw = phase_telemetry if phase_telemetry is not None else PhaseTelemetryWriter(vector_store)
        if error_code:
            pw.phase_error(
                trace_id or "",
                phase or "unknown",
                error_message=error_code,
                success=False,
                repo=repo,
            )
        else:
            pw.phase_complete(
                trace_id or "",
                phase or "unknown",
                latency_ms=latency_ms,
                success=True,
                repo=repo,
            )
    except Exception:  # noqa: BLE001 — soft-fail
        logger.debug("phase_complete/phase_error write failed", exc_info=True)

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
        contract_id=telemetry.contract_id if telemetry else None,
        contract_tags=telemetry.contract_tags if telemetry else None,
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
    if isinstance(resolved_upstream, UpstreamFile) and resolved_upstream.kind == "error":
        detail = resolved_upstream.detail
        assert detail is not None
        return _upstream_parse_error(detail)
    if resolved_upstream is None:
        return _upstream_not_configured_error()
    # At this point resolved_upstream is a tuple (client, url, model).
    # Pyright can't narrow tuple | UpstreamFile | None past the two
    # guards above, so assert it's not UpstreamFile.
    assert not isinstance(resolved_upstream, UpstreamFile)
    upstream_client, chat_url, upstream_model = resolved_upstream

    # Per-session orientation key: explicit harness header (e.g. Claude Code's
    # x-claude-code-session-id), then Qwen Code runtime.json fallback, else
    # the conversation fingerprint. Drives the announce cadence and is stamped
    # onto telemetry.
    session_id = extract_session_header(fastapi_request.headers)
    if not session_id:
        session_id = _fallback_qwen_session_id(cwd)
    session_key, session_source = resolve_session_key(request, session_id)

    # Phase-event writer for this request: prefers the lifespan-scoped instance
    # (task 04) so the schema DDL doesn't re-run every write; reused below for
    # both the signal-layer phase_start (via evaluate_signal) and the llm_*
    # forwarding events.
    phase_telemetry_writer = _get_phase_telemetry_writer(fastapi_request.app, vector_store)

    # --- Step 3: Signal layer ---
    signal_result = None
    composed = False
    try:
        signal_result = await evaluate_signal(
            request,
            cwd,
            embed_client,
            session_id,
            vector_store=vector_store,
            phase_telemetry=phase_telemetry_writer,
        )
    except Exception:
        logger.warning("Signal evaluation failed -- passing through", exc_info=True)

    # --- Step 4: Compose + inject (if signal matched) ---
    # Same `evaluate_signal → compose → inject → commit_markers` cycle as the
    # Anthropic passthrough, via the shared `apply_signal` seam. Three independent
    # legs, in the order they run; `current` threads the request through all three:
    #
    #   leg | content                          | target            | cadence          | gate
    #   ----|----------------------------------|-------------------|------------------|--------------
    #    1  | retrieval-derived compose output | last user message | commit_outcome   | should_compose
    #    2  | per-turn banner                  | last user message | none             | every carrier turn
    #    3  | SDD workflow prose               | system message    | none             | every carrier turn
    #
    # `composed` flips ONLY for leg 1 — legs 2 and 3 are recency/compliance anchors,
    # not compositions, and must not register as one in telemetry. Leg 1's markers are
    # committed only after a confirmed, non-empty injection, so a degraded compose never
    # burns the announce/cursor cadence; legs 2 and 3 commit nothing at all (a cadence
    # marker on leg 3 would recreate the bug #499 fixed — the harness rebuilds every
    # request from its own local history and never observes proxy mutations, so a
    # "delivered once" record would suppress a leg that must fire every turn).
    # The system message is therefore byte-identical WITHIN a phase, changing only on a
    # phase transition, when leg 3 strips the stale block and writes the new one.
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
                # Leg 1 only: the retrieval-derived block goes to the last user
                # message. The system message belongs to leg 3 below, which carries
                # different content (the phase prose) and a different cadence.
                # With no user message there is nowhere to deliver, and returning
                # None keeps the announced marker unburned -- otherwise this turn's
                # commit would permanently forfeit the delivery for the rest of the
                # phase/session.
                if not any(m.role == "user" for m in before.messages):
                    return None
                new_msgs = inject_into_openai_messages(before.messages, text, phase=phase)
                if new_msgs is None:
                    return None
                return before.model_copy(update={"messages": new_msgs})

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

    # Leg 3: SDD workflow prose onto the system message (highest-compliance
    # location). Like the banner it runs on every carrier turn -- outside the
    # compose guard, outside apply_signal -- and commits no cadence marker, so it
    # survives the harness rebuilding its history from scratch each turn. Ordered
    # last: it writes a different message than the banner, so a failure here cannot
    # cost the banner. Must NOT flip `composed`. No system message in the request is
    # a no-op (the helper deliberately does not create one -- the harness owns the
    # message array); the prose is a compliance improvement, not a correctness
    # dependency, and legs 1 and 2 still land on the user message.
    if (
        signal_result is not None
        and signal_result.workflow_system_prose
        and signal_result.phase is not None
    ):
        try:
            sys_msgs = inject_into_openai_system_prompt(
                current.messages, signal_result.workflow_system_prose, phase=signal_result.phase
            )
            if sys_msgs is not None:
                current = current.model_copy(update={"messages": sys_msgs})
        except Exception:
            logger.warning("Workflow prose injection failed -- skipping", exc_info=True)

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
        payload = _build_payload(
            modified_request,
            upstream_model,
            phase=phase,
            free_mode=signal_result.free_mode if signal_result else False,
        )
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

    # llm_sent: emitted ONCE right before dispatch, on both the streaming and
    # non-streaming paths, never blocking the forward.
    trace_id = signal_result.trace_id if signal_result else None
    _emit_llm_sent(
        phase_telemetry_writer,
        trace_id,
        phase,
        upstream_model,
        workflow_skill_id=signal_result.workflow_skill_id if signal_result else None,
        system_prompt_sha=_system_prompt_sha(modified_request),
        repo=repo,
        workflow_delivered=(
            signal_result.workflow_system_prose is not None if signal_result else None
        ),
    )

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
            trace_id=trace_id,
            phase_telemetry=phase_telemetry_writer,
        )
        stream_telemetry = (
            _StreamTelemetry(phase_telemetry_writer, trace_id, phase, upstream_model, repo)
            if phase_telemetry_writer is not None
            else None
        )
        return _stream_upstream_response(
            upstream_client, chat_url, payload, on_status=_commit, telemetry=stream_telemetry
        )

    # Non-streaming: forward and return JSON
    try:
        resp = await upstream_client.post(chat_url, json=payload)
    except httpx.ConnectError as e:
        logger.warning("Upstream connection failed: %s", e)
        error_code = "upstream_connect_error"
        latency_ms = int((time.monotonic() - start_time) * 1000)
        _emit_llm_error(
            phase_telemetry_writer,
            trace_id,
            phase,
            upstream_model,
            error_message=str(e),
            latency_ms=latency_ms,
            repo=repo,
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
            error_code=error_code,
            telemetry=compose_telemetry,
            phase_gate_embed_failed=gate_embed_failed,
            repo=repo,
            session_key=session_key,
            session_source=session_source,
            category=trace_category,
            trace_id=signal_result.trace_id if signal_result else None,
            phase_telemetry=phase_telemetry_writer,
        )
        return _upstream_unavailable_error(str(e))
    except httpx.TimeoutException as e:
        logger.warning("Upstream timeout: %s", e)
        error_code = "upstream_timeout"
        latency_ms = int((time.monotonic() - start_time) * 1000)
        _emit_llm_error(
            phase_telemetry_writer,
            trace_id,
            phase,
            upstream_model,
            error_message=str(e),
            latency_ms=latency_ms,
            repo=repo,
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
            error_code=error_code,
            telemetry=compose_telemetry,
            phase_gate_embed_failed=gate_embed_failed,
            repo=repo,
            session_key=session_key,
            session_source=session_source,
            category=trace_category,
            trace_id=signal_result.trace_id if signal_result else None,
            phase_telemetry=phase_telemetry_writer,
        )
        return _upstream_unavailable_error(str(e))
    except httpx.RequestError as e:
        logger.warning("Upstream request error: %s", e)
        error_code = "upstream_request_error"
        latency_ms = int((time.monotonic() - start_time) * 1000)
        _emit_llm_error(
            phase_telemetry_writer,
            trace_id,
            phase,
            upstream_model,
            error_message=str(e),
            latency_ms=latency_ms,
            repo=repo,
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
            error_code=error_code,
            telemetry=compose_telemetry,
            phase_gate_embed_failed=gate_embed_failed,
            repo=repo,
            session_key=session_key,
            session_source=session_source,
            category=trace_category,
            trace_id=signal_result.trace_id if signal_result else None,
            phase_telemetry=phase_telemetry_writer,
        )
        return _upstream_unavailable_error(str(e))
    except httpx.HTTPError as e:
        logger.warning("Upstream HTTP error: %s", e)
        error_code = "upstream_http_error"
        latency_ms = int((time.monotonic() - start_time) * 1000)
        _emit_llm_error(
            phase_telemetry_writer,
            trace_id,
            phase,
            upstream_model,
            error_message=str(e),
            latency_ms=latency_ms,
            repo=repo,
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
            error_code=error_code,
            telemetry=compose_telemetry,
            phase_gate_embed_failed=gate_embed_failed,
            repo=repo,
            session_key=session_key,
            session_source=session_source,
            category=trace_category,
            trace_id=signal_result.trace_id if signal_result else None,
            phase_telemetry=phase_telemetry_writer,
        )
        return _upstream_unavailable_error(str(e))

    if resp.status_code >= 500:
        logger.warning("Upstream returned HTTP %d: %s", resp.status_code, resp.text[:200])
        error_code = f"upstream_http_{resp.status_code}"
        latency_ms = int((time.monotonic() - start_time) * 1000)
        _emit_llm_error(
            phase_telemetry_writer,
            trace_id,
            phase,
            upstream_model,
            error_message=f"Upstream returned HTTP {resp.status_code}",
            latency_ms=latency_ms,
            repo=repo,
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
            error_code=error_code,
            telemetry=compose_telemetry,
            phase_gate_embed_failed=gate_embed_failed,
            repo=repo,
            session_key=session_key,
            session_source=session_source,
            category=trace_category,
            trace_id=signal_result.trace_id if signal_result else None,
            phase_telemetry=phase_telemetry_writer,
        )
        return _upstream_unavailable_error(f"HTTP {resp.status_code}")

    # Parse and return upstream response
    latency_ms = int((time.monotonic() - start_time) * 1000)
    try:
        body: dict[str, Any] = resp.json()
    except ValueError:
        # Upstream returned 2xx but a non-JSON body — the LLM call itself
        # succeeded (we can't extract tokens_out from a body we can't parse).
        _emit_llm_received(
            phase_telemetry_writer,
            trace_id,
            phase,
            upstream_model,
            tokens_out=None,
            latency_ms=latency_ms,
            repo=repo,
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
            trace_id=signal_result.trace_id if signal_result else None,
            phase_telemetry=phase_telemetry_writer,
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

    _emit_llm_received(
        phase_telemetry_writer,
        trace_id,
        phase,
        upstream_model,
        tokens_out=_extract_tokens_out(body),
        latency_ms=latency_ms,
        repo=repo,
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
        trace_id=trace_id,
        phase_telemetry=phase_telemetry_writer,
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


def _fallback_qwen_session_id(cwd: Path) -> str | None:
    """Read the current Qwen Code session ID from the local runtime.json.

    Qwen Code does not transmit session IDs over HTTP. When no explicit
    session header is present, this function reads the most-recently-modified
    ``runtime.json`` in the project's ``~/.qwen/projects/<encoded-cwd>/chats/``
    directory and returns its ``session_id`` value.

    Returns ``None`` when no runtime file exists or the session ID is empty,
    letting the proxy fall back to fingerprint-based resolution.
    """
    # Qwen Code encodes the project directory as: leading "-" + path parts
    # separated by "-", e.g. "/home/nmeyers/dev/agentalloy" →
    # "-home-nmeyers-dev-agentalloy".
    encoded = "-" + os.path.realpath(os.fspath(cwd)).lstrip("/").replace("/", "-")
    chats_dir = Path.home() / ".qwen" / "projects" / encoded / "chats"
    try:
        runtime_files = list(chats_dir.glob("*.runtime.json"))
    except OSError:
        return None
    if not runtime_files:
        return None
    # Most recently modified runtime.json is the active session.
    runtime_file = max(runtime_files, key=lambda p: p.stat().st_mtime)
    try:
        with runtime_file.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    session_id = data.get("session_id")
    if isinstance(session_id, str) and session_id.strip():
        return session_id.strip()
    return None
