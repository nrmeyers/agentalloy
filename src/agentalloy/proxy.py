"""Token-accurate steering proxy with phase-aware injection.

Intercepts harness LLM traffic, injects steering context, and rewrites
the usage field in responses to include injected tokens. Tracks cumulative
consumption for session summaries.

Phase-aware injection modes:
- Activation turn (phase just changed): inject full persona + contract
- Subsequent turns: inject only relevant skills for THIS turn's prompt
- Skills unchanged: no injection (zero overhead)

Key invariant: the proxy NEVER under-reports tokens. If it injects M tokens
of steering context, the response's prompt_tokens includes M. The harness
sees accurate totals for context window management and session summaries.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections import OrderedDict
from collections.abc import AsyncGenerator
from dataclasses import replace
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from agentalloy.config import Config
from agentalloy.phase_aware_steering import PhaseAwareSteering
from agentalloy.skill_engine import SkillEngine
from agentalloy.state_store import StateStore
from agentalloy.token_counter import TokenCounter
from agentalloy.usage_tracker import UsageTracker

proxy_app = FastAPI(title="AgentAlloy Steering Proxy", version="0.3.0")

_config: Config | None = None
_state_store: StateStore | None = None
_skill_engine: SkillEngine | None = None
_phase_steering: PhaseAwareSteering | None = None
_token_counter: TokenCounter | None = None
_usage_tracker: UsageTracker | None = None
_upstream_url: str = ""
_upstream_key: str = ""
_service_url: str = ""

# Cache: steering context string → token count. Bounded FIFO — steering
# strings are unique per prompt/brief, so an unbounded dict grows forever
# in a long-lived proxy.
_STEERING_TOKEN_CACHE_MAX = 256
_steering_token_cache: OrderedDict[str, int] = OrderedDict()

# Compose verdict cache: prompt sha256 → (context, context_type, expiry).
# The harness fires several chat/completions per user turn (main, title,
# recap, follow-ups); the LFM verdict for a prompt is computed once and
# reused for the rest of the turn instead of paying the full tool loop
# per completion.
_COMPOSE_TTL_SECONDS = 30.0
_COMPOSE_CACHE_MAX = 32

# How long to wait for the service's /compose (the LFM orchestrator tool
# loop). Measured ~20s for a 4-step loop on the 2.6B sidecar — a timeout
# below that silently degrades EVERY turn to static steering, so the LFM's
# contract/skill work never reaches the main model.
import os as _os

# Activation turns with discovery (6-step budget) measured ~42s worst case
# even with the DSpark drafter; 45 left no margin and a timeout silently
# discards the whole compose.
_COMPOSE_TIMEOUT_S = float(_os.environ.get("AGENTALLOY_COMPOSE_TIMEOUT_S", "75"))
_compose_cache: dict[str, tuple[str, int, float]] = {}

# Conversation tracking for session-start detection.
# Key: sha256 of the conversation's FIRST user message (stable across the
# conversation's turns). Value: message counts observed so far. A request
# continues a known conversation only if it has grown beyond a previously
# observed count; anything else — including an identical first request from
# a second session — counts as a session start. A false start only re-
# injects the persona once (the safe direction); a missed start leaves a
# new session un-oriented.
_CONV_TRACK_MAX = 64
_CONV_POINTS_MAX = 8
_conv_counts: OrderedDict[str, list[int]] = OrderedDict()


class _ServicePhaseReader:
    """Duck-typed phase reader for the static steering fallback.

    Reads the current phase from the service's /status instead of opening
    state.duck — the service holds the exclusive file lock.
    """

    def __init__(self, service_url: str):
        self.service_url = service_url

    def get_current_phase(self) -> str:
        try:
            resp = httpx.get(f"{self.service_url}/status", timeout=3.0)
            if resp.status_code == 200:
                phase = resp.json().get("phase")
                if phase:
                    return str(phase)
        except Exception:
            pass
        return "spec"


@proxy_app.on_event("startup")
def startup() -> None:
    """Initialize proxy state."""
    global _config, _state_store, _skill_engine, _phase_steering
    global _token_counter, _usage_tracker, _upstream_url, _upstream_key, _service_url

    _config = Config.from_env()
    _service_url = f"http://127.0.0.1:{_config.service_port}"

    # The service owns the single RW handle to state.duck — DuckDB's file
    # lock is exclusive, so the proxy must not open the file at all. Phase
    # reads for the static fallback go through the service's /status.
    _state_store = None
    _skill_engine = SkillEngine()
    _phase_steering = PhaseAwareSteering(_ServicePhaseReader(_service_url), _skill_engine)
    _token_counter = TokenCounter(
        model_url=_config.upstream_url,
        api_key=_config.upstream_key,
    )
    _usage_tracker = UsageTracker(_config.usage_duck)
    _upstream_url = _config.upstream_url
    _upstream_key = _config.upstream_key


@proxy_app.on_event("shutdown")
def shutdown() -> None:
    """Cleanup. (_state_store is always None by design — the service owns
    the only RW handle to state.duck — so only the usage tracker closes.)"""
    if _usage_tracker:
        _usage_tracker.close()


def _extract_user_prompt(messages: list[dict[str, Any]]) -> str:
    """Extract the last user message content from the messages list."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                # Handle multimodal content (list of parts)
                text_parts = [p.get("text", "") for p in content if isinstance(p, dict)]
                return " ".join(text_parts)
    return ""


def _build_steering_context() -> str:
    """Build the steering context to inject into requests.

    Uses phase-aware steering: activation turns get full persona + contract,
    subsequent turns get only relevant skills (or nothing if unchanged).
    """
    if not _phase_steering:
        return ""
    context, _ = _phase_steering.build_context()
    return context


def _first_user_hash(messages: list[dict[str, Any]]) -> str:
    """Stable identity for a conversation: hash of its first user message
    plus the leading system prompt. Including the system prompt keeps
    different request kinds that share a first user message (main session
    vs auxiliary completions) from colliding on one conversation slot."""
    system = ""
    if messages and messages[0].get("role") == "system":
        sys_content = messages[0].get("content", "")
        if isinstance(sys_content, list):
            sys_content = " ".join(
                p.get("text", "") for p in sys_content if isinstance(p, dict)
            )
        system = str(sys_content)
    for msg in messages:
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    p.get("text", "") for p in content if isinstance(p, dict)
                )
            return hashlib.sha256(f"{system}\x00{content}".encode()).hexdigest()
    return ""


def _detect_session_start(messages: list[dict[str, Any]]) -> bool:
    """True when this request begins a NEW conversation (session start).

    The harness exposes no session id on chat/completions, so session start
    is inferred from conversation shape: a request continues a known
    conversation only if its first user message matches and its message
    list has grown beyond a previously observed count of that conversation.
    Two sessions starting with an identical first prompt are
    indistinguishable on request 1 and each register as a start — the
    safe direction, since a false start only re-injects the persona.
    """
    h = _first_user_hash(messages)
    if not h:
        return False
    n = len(messages)
    counts = _conv_counts.get(h)
    is_start = counts is None or all(n <= c for c in counts)
    if counts is None:
        _conv_counts[h] = [n]
    else:
        counts.append(n)
        if len(counts) > _CONV_POINTS_MAX:
            counts.pop(0)
    _conv_counts.move_to_end(h)
    while len(_conv_counts) > _CONV_TRACK_MAX:
        _conv_counts.popitem(last=False)
    return is_start


def _compose_cache_key(prompt: str, new_session: bool, session_key: str, project: str) -> str:
    """Compose verdict cache key: prompt + session-start flag + conversation."""
    parts = f"{prompt}\x00{'new' if new_session else 'turn'}\x00{session_key}\x00{project}"
    return hashlib.sha256(parts.encode()).hexdigest()


def _compose_verdict_sync(
    prompt: str, new_session: bool, session_key: str, project: str = ""
) -> tuple[str, int]:
    """Blocking compose: service /compose (LFM tool loop) with static fallback.

    new_session marks a freshly detected conversation start — the service
    forces the activation branch (persona + brief) for it regardless of
    phase. session_key is the conversation hash so the service tracks phase
    per conversation. Runs in a worker thread (see _build_turn_context) —
    the HTTP calls here must not block the proxy's event loop.
    """
    if _service_url:
        try:
            resp = httpx.post(
                f"{_service_url}/compose",
                json={
                    "prompt": prompt,
                    "new_session": new_session,
                    "session_key": session_key,
                    "project": project,
                },
                timeout=_COMPOSE_TIMEOUT_S,
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("context", ""), int(data.get("context_type", 0))
        except Exception:
            pass  # service down → static fallback below

    if not _phase_steering:
        return "", 0
    return _phase_steering.build_context(prompt=prompt, is_activation=new_session)


async def _build_turn_context(
    messages: list[dict[str, Any]], project: str = ""
) -> tuple[str, int]:
    """Build steering context for this turn.

    The compose verdict is cached per (user prompt, session-start flag)
    with TTL 30s: the harness fires several completions per user turn
    (main, title, recap, follow-ups) and only the first pays the LFM tool
    loop. A freshly detected conversation start is flagged new_session so
    the service forces the activation branch (persona + brief) for it.

    Primary path: the service's /compose endpoint, where LFM runs its tool
    loop (contracts, skills, phase state) and writes the steering brief.
    Fallback: local static phase-aware steering if the service is unreachable.

    Returns:
        (context_string, context_type)
        context_type: 0 = no injection, 1 = turn brief, 2 = activation
    """
    prompt = _extract_user_prompt(messages)
    if not prompt:
        return "", 0

    new_session = _detect_session_start(messages)
    session_key = _first_user_hash(messages)

    now = time.monotonic()
    key = _compose_cache_key(prompt, new_session, session_key, project)
    cached = _compose_cache.get(key)
    if cached is not None:
        if now < cached[2]:
            return cached[0], cached[1]
        del _compose_cache[key]

    context, context_type = await asyncio.to_thread(
        _compose_verdict_sync, prompt, new_session, session_key, project
    )

    if len(_compose_cache) >= _COMPOSE_CACHE_MAX:
        # Expire stale entries first; evict oldest only if still over —
        # clearing wholesale would also drop fresh verdicts mid-turn.
        cutoff = time.monotonic()
        for stale in [k for k, v in _compose_cache.items() if v[2] <= cutoff]:
            del _compose_cache[stale]
        while len(_compose_cache) >= _COMPOSE_CACHE_MAX:
            del _compose_cache[next(iter(_compose_cache))]
    _compose_cache[key] = (context, context_type, now + _COMPOSE_TTL_SECONDS)
    return context, context_type


def _count_steering_tokens(steering: str) -> int:
    """Count tokens in steering context, with caching.

    Blocking (HTTP /tokenize on cache miss) — call via asyncio.to_thread
    from async handlers, never directly on the event loop.
    """
    if steering in _steering_token_cache:
        return _steering_token_cache[steering]

    count = _token_counter.count(steering) if _token_counter else 0
    _steering_token_cache[steering] = count
    while len(_steering_token_cache) > _STEERING_TOKEN_CACHE_MAX:
        _steering_token_cache.popitem(last=False)
    return count


def _inject_steering(
    messages: list[dict[str, Any]], steering: str
) -> tuple[list[dict[str, Any]], str]:
    """Merge steering into the leading system message.

    The Qwen chat template has a single system slot at messages[0]; a second
    system message is rejected by vLLM ("System message must be at the
    beginning"). So steering is appended to the harness's existing leading
    system message instead of prepended as a new one.

    Returns (new_messages, appended_text) where appended_text is exactly
    what was added — count it for token accounting so usage never
    under-reports.
    """
    if messages and messages[0].get("role") == "system":
        first = dict(messages[0])
        content = first.get("content", "")
        if isinstance(content, str):
            appended = f"\n\n{steering}" if content else steering
            first["content"] = content + appended
        elif isinstance(content, list):
            first["content"] = list(content) + [{"type": "text", "text": steering}]
            appended = steering
        else:
            first["content"] = steering
            appended = steering
        return [first] + messages[1:], appended
    return [{"role": "system", "content": steering}] + messages, steering


def _rewrite_usage(
    usage: dict[str, Any],
    injected_tokens: int,
) -> dict[str, Any]:
    """Add injected tokens to the usage report."""
    result = dict(usage)
    result["prompt_tokens"] = result.get("prompt_tokens", 0) + injected_tokens
    result["total_tokens"] = result.get("total_tokens", 0) + injected_tokens
    result["agentalloy_injected_tokens"] = injected_tokens
    return result


def _track_usage(usage: dict[str, Any], injected_tokens: int) -> None:
    """Record usage in the tracker."""
    if _usage_tracker:
        _usage_tracker.record(
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            injected_tokens=injected_tokens,
        )


def _is_auxiliary_request(data: dict[str, Any]) -> bool:
    """True for harness side-completions (title, recap, follow-ups).

    Those are one-shots that must not receive steering: injecting the
    activation persona contaminates their output, and registering them as
    conversations churns the session-tracking LRU. Heuristics: a small
    completion budget, or no leading system prompt.
    """
    max_tokens = data.get("max_tokens") or data.get("max_completion_tokens")
    if isinstance(max_tokens, int) and max_tokens <= 512:
        return True
    messages = data.get("messages") or []
    return not messages or messages[0].get("role") != "system"


def _extract_sse_usage(line: str) -> dict[str, Any] | None:
    """The usage object from an SSE data line, if it carries one."""
    if not line.startswith("data: ") or line == "data: [DONE]":
        return None
    try:
        usage = json.loads(line[6:]).get("usage")
    except json.JSONDecodeError:
        return None
    return usage if isinstance(usage, dict) else None


def _rewrite_sse_line(line: str, injected_tokens: int) -> str:
    """Rewrite a single SSE data line if it contains usage."""
    if not line.startswith("data: ") or line == "data: [DONE]":
        return line
    try:
        payload = line[6:]
        data = json.loads(payload)
        if "usage" in data and data["usage"]:
            data["usage"] = _rewrite_usage(data["usage"], injected_tokens)
            return f"data: {json.dumps(data)}"
        return line
    except (json.JSONDecodeError, KeyError):
        return line


# --- Routes registered BEFORE the catch-all ---


@proxy_app.get("/usage")
def get_usage() -> Response:
    """Session-level cumulative token usage."""
    if not _usage_tracker:
        return Response(
            content=json.dumps({"error": "usage tracker not initialized"}),
            status_code=503,
        )
    totals = _usage_tracker.get_totals()
    return Response(
        content=json.dumps(totals),
        media_type="application/json",
    )


@proxy_app.get("/usage/history")
def get_usage_history(limit: int = 50) -> Response:
    """Per-request usage history."""
    if not _usage_tracker:
        return Response(
            content=json.dumps({"error": "usage tracker not initialized"}),
            status_code=503,
        )
    history = _usage_tracker.get_history(limit)
    records = [
        {
            "prompt_tokens": r.prompt_tokens,
            "completion_tokens": r.completion_tokens,
            "injected_tokens": r.injected_tokens,
            "timestamp": r.timestamp,
        }
        for r in history
    ]
    return Response(
        content=json.dumps({"history": records}),
        media_type="application/json",
    )


# --- Model listing (inject proxy model into upstream's list) ---


@proxy_app.get("/v1/models")
@proxy_app.get("/p/{project}/v1/models")
async def list_models(project: str = "") -> Response:
    """Return upstream models + agentalloy-proxy (same list on the
    project-prefixed base URL the wired harness validates against)."""
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                f"{_upstream_url}/v1/models",
                headers={"authorization": f"Bearer {_upstream_key}"} if _upstream_key else {},
                timeout=10.0,
            )
            data = resp.json()
        except Exception:
            data = {"object": "list", "data": []}

    # Inject the proxy model
    proxy_model = {
        "id": "agentalloy-proxy",
        "object": "model",
        "owned_by": "agentalloy",
    }
    data.setdefault("data", []).insert(0, proxy_model)

    return Response(
        content=json.dumps(data),
        media_type="application/json",
    )


# --- Upstream config (hot-swap + persistence to env.sh) ---


@proxy_app.get("/upstream")
def get_upstream() -> dict[str, Any]:
    """The upstream LLM the proxy is currently forwarding to."""
    return {
        "upstream_url": _upstream_url,
        "model": _config.model if _config else "",
        "key_configured": bool(_upstream_key),
    }


class UpstreamRequest(BaseModel):
    url: str
    model: str
    key: str = ""


@proxy_app.post("/upstream")
async def set_upstream(request: UpstreamRequest) -> JSONResponse:
    """Hot-swap the upstream LLM and persist it to the instance env.sh.

    Validates the new upstream first (GET /v1/models) so a bad URL, key, or
    model name can never strand the proxy. Omitting the key keeps the
    current one.
    """
    global _config, _upstream_url, _upstream_key

    url = request.url.rstrip("/")
    # The stored key is only ever sent to the URL it was configured for —
    # falling back to it for a NEW url would hand the credential to any
    # endpoint a caller posts (this route is unauthenticated).
    key = request.key or (_upstream_key if url == _upstream_url.rstrip("/") else "")

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                f"{url}/v1/models",
                headers={"authorization": f"Bearer {key}"} if key else {},
                timeout=10.0,
            )
        except httpx.HTTPError as e:
            return JSONResponse(
                status_code=502,
                content={"status": "error", "message": f"cannot reach {url}: {e}"},
            )

    if resp.status_code in (401, 403):
        return JSONResponse(
            status_code=400,
            content={
                "status": "error",
                "message": f"upstream rejected the API key (HTTP {resp.status_code})",
            },
        )
    if resp.status_code != 200:
        return JSONResponse(
            status_code=502,
            content={
                "status": "error",
                "message": f"upstream returned HTTP {resp.status_code} from /v1/models",
            },
        )

    try:
        ids = [m.get("id", "") for m in resp.json().get("data", [])]
    except (json.JSONDecodeError, AttributeError):
        ids = []
    if request.model not in ids:
        return JSONResponse(
            status_code=400,
            content={
                "status": "error",
                "message": f"model {request.model!r} is not served by {url}",
                "available": ids,
            },
        )

    # Swap the live config — the catch-all route reads it per request.
    _upstream_url = url
    _upstream_key = key
    if _config is not None:
        _config = replace(
            _config, model=request.model, upstream_url=url, upstream_key=key
        )
    if _token_counter is not None:
        _token_counter.model_url = url
        _token_counter.api_key = key

    # Persist so a proxy restart keeps the new upstream.
    persisted = False
    if _config is not None:
        try:
            from agentalloy.instance_env import instance_env_path, update_env_vars

            persisted = update_env_vars(
                instance_env_path(_config.state_duck),
                {
                    "AGENTALLOY_UPSTREAM_URL": url,
                    "AGENTALLOY_MODEL": request.model,
                    "AGENTALLOY_UPSTREAM_KEY": key,
                },
            )
        except OSError:
            persisted = False

    return JSONResponse(
        content={
            "status": "ok",
            "upstream_url": url,
            "model": request.model,
            "persisted": persisted,
        }
    )


# --- Catch-all proxy route (MUST be last) ---

# Project-scoped base URL prefix: `agentalloy wire` points the harness at
# /p/<project-key>/v1 so every request self-identifies its project. The
# prefix is stripped before the upstream hop; the key scopes compose state.
_PROJECT_PREFIX_RE = re.compile(r"^p/([A-Za-z0-9_.-]+)/(.*)$")


@proxy_app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_request(request: Request, path: str) -> Response:
    """Proxy all requests to upstream, injecting steering for chat completions."""
    project = ""
    m = _PROJECT_PREFIX_RE.match(path)
    if m:
        project, path = m.group(1), m.group(2)
    url = f"{_upstream_url}/{path}"

    headers = dict(request.headers)
    headers.pop("host", None)
    headers.pop("content-length", None)

    body = await request.body()
    injected_tokens = 0
    is_streaming = False
    is_chat = path.endswith("chat/completions") and request.method == "POST"

    if is_chat:
        try:
            data = json.loads(body)
            is_streaming = data.get("stream", False)
            messages = data.get("messages", [])
            mutated = False

            # Rewrite model name: harness sends "agentalloy-proxy" but
            # the upstream needs the real model name it's serving
            model_name = data.get("model", "")
            if model_name == "agentalloy-proxy" and _config:
                data["model"] = _config.model
                mutated = True

            # Phase-aware steering: activation vs turn-based injection.
            # Auxiliary one-shots (title/recap/follow-up) get none — the
            # persona contaminates their output, and registering them as
            # conversations churns the session-tracking LRU.
            if _is_auxiliary_request(data):
                steering = ""
            else:
                steering, _context_type = await _build_turn_context(messages, project)

            if steering:
                # Merge into the leading system message — a second system
                # message breaks vLLM's single-system-slot validation.
                data["messages"], appended = _inject_steering(messages, steering)

                # Token counting does HTTP on cache miss — off the loop.
                injected_tokens = await asyncio.to_thread(_count_steering_tokens, appended)

                mutated = True

            # Always request usage in the final streamed chunk: the tracker
            # must see streamed completions too (the main session traffic),
            # steering or not.
            if is_streaming:
                data.setdefault("stream_options", {})["include_usage"] = True
                mutated = True

            # Re-serialize whenever the payload changed — the original bytes
            # still carry the "agentalloy-proxy" alias
            if mutated:
                body = json.dumps(data).encode()
                headers["content-length"] = str(len(body))
        except (json.JSONDecodeError, KeyError):
            pass

    # The upstream hop authenticates with the configured upstream key, never
    # the harness's inbound credential (a placeholder like "sk-dummy" would
    # shadow the real key and break auth after an /upstream hot-swap).
    headers.pop("authorization", None)
    if _upstream_key:
        headers["authorization"] = f"Bearer {_upstream_key}"

    # Streaming: use client.stream() for true incremental forwarding
    if is_streaming:
        return await _proxy_streaming(url, headers, body, injected_tokens, track=is_chat)

    # Non-streaming: buffer response, rewrite usage
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.request(
                method=request.method,
                url=url,
                headers=headers,
                content=body,
                timeout=120.0,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout):
            # One retry on transient connect failures; no bytes were sent
            # upstream successfully, so the request is safe to repeat.
            resp = await client.request(
                method=request.method,
                url=url,
                headers=headers,
                content=body,
                timeout=120.0,
            )

    resp_headers = dict(resp.headers)
    resp_headers.pop("content-encoding", None)
    resp_headers.pop("transfer-encoding", None)
    resp_headers.pop("content-length", None)

    if is_chat:
        try:
            resp_data = resp.json()
            usage = resp_data.get("usage") if isinstance(resp_data, dict) else None
            if isinstance(usage, dict):
                if injected_tokens > 0:
                    usage = _rewrite_usage(usage, injected_tokens)
                    resp_data["usage"] = usage
                # Record every chat completion, injected or not.
                await asyncio.to_thread(_track_usage, usage, injected_tokens)
                if injected_tokens > 0:
                    content = json.dumps(resp_data).encode()
                    resp_headers["content-length"] = str(len(content))
                    return Response(
                        content=content,
                        status_code=resp.status_code,
                        headers=resp_headers,
                    )
        except (json.JSONDecodeError, KeyError):
            pass

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=resp_headers,
    )


async def _proxy_streaming(
    url: str,
    headers: dict[str, str],
    body: bytes,
    injected_tokens: int,
    track: bool = False,
) -> Response:
    """Stream SSE from upstream, rewriting usage in the final chunk.

    The upstream response is opened before the StreamingResponse is built:
    Starlette sends http.response.start before iterating the body, so the
    real upstream status must be known up front. Error payloads (>=400) are
    forwarded with their original status as application/json instead of
    being masked as a 200 event stream.
    """
    client = httpx.AsyncClient(timeout=120.0)
    request = client.build_request("POST", url, headers=headers, content=body)
    try:
        try:
            upstream = await client.send(request, stream=True)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            # One retry on transient connect failures (nothing streamed yet).
            upstream = await client.send(request, stream=True)
    except Exception:
        await client.aclose()
        raise

    async def generate() -> AsyncGenerator[bytes, None]:
        buffer = ""

        async def handle(line: str) -> str:
            """Rewrite the usage line and record it (streamed completions
            are the main traffic — they must land in the tracker too)."""
            if '"usage"' not in line:
                return line
            if injected_tokens > 0:
                line = _rewrite_sse_line(line, injected_tokens)
            if track:
                usage = _extract_sse_usage(line)
                if usage:
                    await asyncio.to_thread(_track_usage, usage, injected_tokens)
            return line

        try:
            async for chunk in upstream.aiter_text():
                buffer += chunk
                # Process complete lines only
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = await handle(line)
                    yield (line + "\n").encode()
            # Flush remaining buffer
            if buffer.strip():
                buffer = await handle(buffer)
                yield buffer.encode()
        finally:
            await upstream.aclose()
            await client.aclose()

    media_type = "application/json" if upstream.status_code >= 400 else "text/event-stream"
    return StreamingResponse(
        generate(),
        status_code=upstream.status_code,
        media_type=media_type,
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )
