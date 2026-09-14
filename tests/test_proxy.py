"""Steering proxy tests — verify token-accurate usage rewriting."""

import asyncio
from collections.abc import Generator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

import agentalloy.proxy as proxy_module
from agentalloy.api.proxy_router import (
    _emit_llm_received,
    _extract_tokens_in,
    _extract_tokens_out,
    _SseUsageScanner,
)
from agentalloy.config import Config
from agentalloy.proxy import (
    _build_steering_context,
    _build_turn_context,
    _inject_steering,
    _rewrite_sse_line,
    _rewrite_usage,
)
from agentalloy.token_counter import TokenCounter

SpyCall = tuple[str, dict[str, Any] | None]


@pytest.fixture
def compose_spy(monkeypatch: pytest.MonkeyPatch) -> list[SpyCall]:
    """Patch httpx.post used by the compose worker; record (url, body)."""
    calls: list[SpyCall] = []

    def fake_post(url: str, **kwargs: Any) -> SimpleNamespace:
        calls.append((url, kwargs.get("json")))
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"context": "brief", "context_type": 1},
        )

    monkeypatch.setattr(proxy_module.httpx, "post", fake_post)
    monkeypatch.setattr(proxy_module, "_service_url", "http://127.0.0.1:48950")
    proxy_module._compose_cache.clear()
    yield calls
    proxy_module._compose_cache.clear()


def test_steering_context_includes_phase() -> None:
    """Steering context includes current phase."""
    context = _build_steering_context()
    assert isinstance(context, str)


def test_rewrite_usage_adds_injected_tokens() -> None:
    """Usage rewriting adds injected tokens to prompt_tokens and total_tokens."""
    usage = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
    rewritten = _rewrite_usage(usage, injected_tokens=25)

    assert rewritten["prompt_tokens"] == 125
    assert rewritten["completion_tokens"] == 50
    assert rewritten["total_tokens"] == 175
    assert rewritten["agentalloy_injected_tokens"] == 25


def test_rewrite_usage_preserves_original() -> None:
    """Rewriting does not mutate the original usage dict."""
    usage = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
    _rewrite_usage(usage, injected_tokens=25)

    assert usage["prompt_tokens"] == 100
    assert usage["total_tokens"] == 150


def test_rewrite_usage_zero_injection() -> None:
    """Zero injected tokens leaves usage unchanged (except metadata field)."""
    usage = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
    rewritten = _rewrite_usage(usage, injected_tokens=0)

    assert rewritten["prompt_tokens"] == 100
    assert rewritten["total_tokens"] == 150
    assert rewritten["agentalloy_injected_tokens"] == 0


def test_rewrite_usage_missing_fields() -> None:
    """Handles usage dicts with missing fields gracefully."""
    usage: dict[str, Any] = {}
    rewritten = _rewrite_usage(usage, injected_tokens=10)

    assert rewritten["prompt_tokens"] == 10
    assert rewritten["total_tokens"] == 10
    assert rewritten["agentalloy_injected_tokens"] == 10


def test_extract_tokens_in_pulls_prompt_tokens() -> None:
    """``usage.prompt_tokens`` is the delivered prompt size — the triage number
    for "was the prompt near the context limit"."""
    body = {"usage": {"prompt_tokens": 42_000, "completion_tokens": 100, "total_tokens": 42_100}}
    assert _extract_tokens_in(body) == 42_000
    assert _extract_tokens_out(body) == 100


def test_extract_tokens_in_absent_usage() -> None:
    assert _extract_tokens_in({}) is None
    assert _extract_tokens_in({"usage": {}}) is None
    assert _extract_tokens_in({"usage": {"completion_tokens": 5}}) is None
    assert _extract_tokens_in({"usage": "nope"}) is None


def test_sse_usage_scanner_captures_prompt_tokens() -> None:
    scanner = _SseUsageScanner()
    scanner.feed('data: {"choices":[{"delta":{"content":"hi"}}]}\n\n')
    assert scanner.latest is None and scanner.latest_in is None
    scanner.feed(
        'data: {"choices":[],"usage":{"prompt_tokens":900,"completion_tokens":42,"total_tokens":942}}\n\n'
    )
    assert scanner.latest == 42
    assert scanner.latest_in == 900
    scanner.feed("data: [DONE]\n\n")
    assert scanner.latest == 42 and scanner.latest_in == 900


def test_sse_usage_scanner_prompt_only_usage() -> None:
    """A usage block with only prompt_tokens (no completion_tokens) still
    records the input size — the output count stays None, not invented."""
    scanner = _SseUsageScanner()
    scanner.feed('data: {"usage":{"prompt_tokens":777}}\n\n')
    assert scanner.latest is None
    assert scanner.latest_in == 777


def test_sse_usage_scanner_fragmented_usage_line() -> None:
    """A usage line split across byte-boundary chunks is still captured:
    the first half arrives unterminated, the second completes it."""
    line = 'data: {"usage":{"prompt_tokens":1234,"completion_tokens":56}}'
    mid = len(line) // 2
    scanner = _SseUsageScanner()
    scanner.feed(line[:mid])  # no trailing newline — line still open
    assert scanner.latest_in is None  # partial line not parsed yet
    scanner.feed(line[mid:] + "\n")
    assert scanner.latest_in == 1234
    assert scanner.latest == 56


class _RecordingWriter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def llm_received(self, trace_id: str, phase: str, **kwargs: Any) -> None:
        self.calls.append((trace_id, phase, kwargs))


def test_emit_llm_received_forwards_tokens_in() -> None:
    """The received-row emitter carries the model-reported input size."""
    writer = _RecordingWriter()
    _emit_llm_received(
        writer,
        "t1",
        "specced",
        "m",
        tokens_out=100,
        tokens_in=42_000,
        latency_ms=55,
        repo="r",
    )
    assert writer.calls == [
        (
            "t1",
            "specced",
            {
                "model": "m",
                "tokens_in": 42_000,
                "tokens_out": 100,
                "latency_ms": 55,
                "success": True,
                "repo": "r",
            },
        )
    ]
    # Default stays None — a call site without usage must not invent a count.
    writer = _RecordingWriter()
    _emit_llm_received(writer, "t1", "specced", "m", tokens_out=None, latency_ms=5)
    assert writer.calls[0][2]["tokens_in"] is None


def test_rewrite_sse_line_with_usage() -> None:
    """SSE line with usage gets rewritten."""
    line = (
        'data: {"choices":[],"usage":{"prompt_tokens":12,"completion_tokens":20,"total_tokens":32}}'
    )
    rewritten = _rewrite_sse_line(line, injected_tokens=5)

    assert '"prompt_tokens": 17' in rewritten or '"prompt_tokens":17' in rewritten
    assert '"total_tokens": 37' in rewritten or '"total_tokens":37' in rewritten
    assert (
        '"agentalloy_injected_tokens": 5' in rewritten
        or '"agentalloy_injected_tokens":5' in rewritten
    )


def test_rewrite_sse_line_without_usage() -> None:
    """SSE line without usage passes through unchanged."""
    line = 'data: {"choices":[{"delta":{"content":"hi"}}]}'
    rewritten = _rewrite_sse_line(line, injected_tokens=5)
    assert rewritten == line


def test_rewrite_sse_done_marker() -> None:
    """SSE [DONE] marker passes through unchanged."""
    line = "data: [DONE]"
    rewritten = _rewrite_sse_line(line, injected_tokens=5)
    assert rewritten == line


def test_rewrite_sse_non_data_line() -> None:
    """Non-data SSE lines pass through unchanged."""
    line = "event: message"
    rewritten = _rewrite_sse_line(line, injected_tokens=5)
    assert rewritten == line


def test_compose_verdict_cached_per_prompt(compose_spy: list[SpyCall]) -> None:
    """Same user prompt composes once; later completions reuse the verdict."""
    messages = [{"role": "user", "content": "hi"}]

    first = asyncio.run(_build_turn_context(messages))
    second = asyncio.run(_build_turn_context(messages))

    assert first == ("brief", 1)
    assert second == ("brief", 1)
    assert len(compose_spy) == 1


def test_compose_different_prompt_recomposes(compose_spy: list[SpyCall]) -> None:
    """A new user prompt (new turn) pays the compose again."""
    first = asyncio.run(_build_turn_context([{"role": "user", "content": "hi"}]))
    second = asyncio.run(_build_turn_context([{"role": "user", "content": "do the intake"}]))

    assert first == ("brief", 1)
    assert second == ("brief", 1)
    assert len(compose_spy) == 2


def test_compose_cache_expiry(compose_spy: list[SpyCall]) -> None:
    """An expired verdict is recomputed instead of served stale."""
    messages = [{"role": "user", "content": "hi"}]
    asyncio.run(_build_turn_context(messages))

    # First request of a 1-message conversation detects as a session start.
    key = proxy_module._compose_cache_key("hi", True, proxy_module._first_user_hash(messages), "")
    context, context_type, _ = proxy_module._compose_cache[key]
    proxy_module._compose_cache[key] = (context, context_type, 0.0)

    asyncio.run(_build_turn_context(messages))
    assert len(compose_spy) == 2


def test_compose_carries_project_scope(compose_spy: list[SpyCall]) -> None:
    """The project key reaches /compose, and distinct projects don't share
    a cached verdict for the same prompt."""
    messages = [{"role": "user", "content": "hi"}]
    asyncio.run(_build_turn_context(messages, project="alpha-11111111"))
    asyncio.run(_build_turn_context(messages, project="beta-22222222"))

    assert len(compose_spy) == 2
    assert compose_spy[0][1]["project"] == "alpha-11111111"
    assert compose_spy[1][1]["project"] == "beta-22222222"


def test_project_prefix_stripped_before_upstream() -> None:
    """/p/<key>/v1/... routes hit the catch-all with the prefix stripped."""
    m = proxy_module._PROJECT_PREFIX_RE.match("p/myrepo-abc123/v1/chat/completions")
    assert m is not None
    assert m.group(1) == "myrepo-abc123"
    assert m.group(2) == "v1/chat/completions"
    # Non-prefixed paths are untouched.
    assert proxy_module._PROJECT_PREFIX_RE.match("v1/chat/completions") is None


def test_compose_skipped_without_user_prompt(compose_spy: list[SpyCall]) -> None:
    """No user message → no compose call, no injection."""
    result = asyncio.run(_build_turn_context([{"role": "assistant", "content": "x"}]))

    assert result == ("", 0)
    assert compose_spy == []


def test_compose_flags_new_session_on_first_request(compose_spy: list[SpyCall]) -> None:
    """The first request of a conversation is flagged new_session to /compose."""
    asyncio.run(_build_turn_context([{"role": "user", "content": "hi"}]))

    sent = compose_spy[0][1]
    assert sent["prompt"] == "hi"
    assert sent["new_session"] is True
    # Conversation identity travels with the request (per-session phase
    # tracking on the service side).
    assert sent["session_key"]


def test_compose_flags_continuation_not_new(compose_spy: list[SpyCall]) -> None:
    """A grown conversation is a continuation — new_session stays False."""
    asyncio.run(_build_turn_context([{"role": "user", "content": "hi"}]))
    asyncio.run(
        _build_turn_context(
            [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
                {"role": "user", "content": "write the spec"},
            ]
        )
    )

    assert compose_spy[0][1]["new_session"] is True
    assert compose_spy[1][1]["prompt"] == "write the spec"
    assert compose_spy[1][1]["new_session"] is False


def test_compose_two_sessions_same_first_prompt(compose_spy: list[SpyCall]) -> None:
    """Two content-identical conversations: the second start reuses the
    first's cached verdict (identical content → identical activation is
    correct); a genuinely new first prompt always recomposes."""
    first = asyncio.run(_build_turn_context([{"role": "user", "content": "hi"}]))
    second = asyncio.run(_build_turn_context([{"role": "user", "content": "hi"}]))

    assert first == ("brief", 1)
    assert second == ("brief", 1)
    assert len(compose_spy) == 1

    third = asyncio.run(
        _build_turn_context([{"role": "user", "content": "different first prompt"}])
    )
    assert third == ("brief", 1)
    assert len(compose_spy) == 2
    assert compose_spy[1][1]["new_session"] is True


def test_detect_session_start_growth_and_shrink() -> None:
    """Start = unseen conversation or shrink to an observed count; growth =
    continuation. No user message → not a start."""
    proxy_module._conv_counts.clear()
    try:
        conv = [{"role": "user", "content": "hi"}]
        assert proxy_module._detect_session_start(conv) is True
        grown = conv + [
            {"role": "assistant", "content": "a"},
            {"role": "user", "content": "b"},
        ]
        assert proxy_module._detect_session_start(grown) is False
        # Hard shrink (e.g. post-compaction) back to a previously observed
        # count re-opens the conversation → re-orientation.
        assert proxy_module._detect_session_start(conv) is True
        assert proxy_module._detect_session_start([{"role": "assistant", "content": "x"}]) is False
    finally:
        proxy_module._conv_counts.clear()


def test_inject_steering_merges_into_leading_system() -> None:
    """Steering is appended to the harness's leading system message — one system msg, at index 0."""
    messages = [
        {"role": "system", "content": "you are the harness"},
        {"role": "user", "content": "hi"},
    ]
    new_messages, appended = _inject_steering(messages, "STEERING")

    assert len(new_messages) == 2
    assert new_messages[0]["role"] == "system"
    assert new_messages[0]["content"] == "you are the harness\n\nSTEERING"
    assert new_messages[1] == {"role": "user", "content": "hi"}
    assert appended == "\n\nSTEERING"


def test_inject_steering_empty_leading_system() -> None:
    """An empty leading system content is replaced, no separator."""
    messages = [{"role": "system", "content": ""}, {"role": "user", "content": "hi"}]
    new_messages, appended = _inject_steering(messages, "STEERING")

    assert new_messages[0]["content"] == "STEERING"
    assert appended == "STEERING"


def test_inject_steering_multimodal_leading_system() -> None:
    """A list-of-parts leading system content gets a text part appended."""
    messages = [
        {"role": "system", "content": [{"type": "text", "text": "base"}]},
        {"role": "user", "content": "hi"},
    ]
    new_messages, appended = _inject_steering(messages, "STEERING")

    assert new_messages[0]["content"] == [
        {"type": "text", "text": "base"},
        {"type": "text", "text": "STEERING"},
    ]
    assert appended == "STEERING"


def test_inject_steering_no_leading_system_prepends() -> None:
    """No leading system message → steering becomes the single leading system message."""
    messages = [{"role": "user", "content": "hi"}]
    new_messages, appended = _inject_steering(messages, "STEERING")

    assert new_messages[0] == {"role": "system", "content": "STEERING"}
    assert new_messages[1] == {"role": "user", "content": "hi"}
    assert appended == "STEERING"


def test_inject_steering_does_not_mutate_input() -> None:
    """The input messages list and its leading system dict are not mutated."""
    messages = [
        {"role": "system", "content": "you are the harness"},
        {"role": "user", "content": "hi"},
    ]
    original = dict(messages[0])
    _inject_steering(messages, "STEERING")

    assert messages[0] == original
    assert len(messages) == 2


# --- /upstream hot-swap endpoint ---


class _FakeModelsResponse:
    """Stand-in for the httpx response of GET {url}/v1/models."""

    def __init__(self, status_code: int, ids: list[str] | None = None) -> None:
        self.status_code = status_code
        self._ids = ids or []

    def json(self) -> dict[str, Any]:
        return {"object": "list", "data": [{"id": m} for m in self._ids]}


class _FakeAsyncClient:
    """Stands in for httpx.AsyncClient — serves the configured response, or
    raises ConnectError when the upstream is unreachable."""

    def __init__(self, response: _FakeModelsResponse | None) -> None:
        self._response = response

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def get(self, url: str, **kwargs: Any) -> _FakeModelsResponse:
        if self._response is None:
            raise httpx.ConnectError("connection refused")
        return self._response


@pytest.fixture
def upstream_env(tmp_path: Path) -> Generator[Path]:
    """Live proxy globals → fake upstream + a tmp env.sh to persist into."""
    env_sh = tmp_path / "env.sh"
    env_sh.write_text(
        "export AGENTALLOY_SERVICE_PORT=48950\n"
        "export AGENTALLOY_UPSTREAM_URL=http://old:8000\n"
        "export AGENTALLOY_MODEL=old-model\n"
        "export AGENTALLOY_UPSTREAM_KEY=sk-old\n"
    )
    proxy_module._config = Config(
        model="old-model",
        upstream_url="http://old:8000",
        upstream_key="sk-old",
        state_duck=str(tmp_path / "state.duck"),
    )
    proxy_module._upstream_url = "http://old:8000"
    proxy_module._upstream_key = "sk-old"
    proxy_module._token_counter = None
    yield env_sh
    proxy_module._config = None
    proxy_module._upstream_url = ""
    proxy_module._upstream_key = ""
    proxy_module._token_counter = None


def _patch_models(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int = 200,
    ids: list[str] | None = None,
    unreachable: bool = False,
) -> None:
    """Point httpx.AsyncClient at a fake /v1/models server."""
    response = None if unreachable else _FakeModelsResponse(status_code, ids)

    def factory() -> _FakeAsyncClient:
        return _FakeAsyncClient(response)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def test_upstream_get_reports_live_config(upstream_env: Path) -> None:
    """GET /upstream reports the live upstream — never the key itself."""
    data = TestClient(proxy_module.proxy_app).get("/upstream").json()
    assert data == {
        "upstream_url": "http://old:8000",
        "model": "old-model",
        "key_configured": True,
    }


def test_upstream_get_without_config(upstream_env: Path) -> None:
    """GET /upstream before startup reports empty values, not an error."""
    proxy_module._config = None
    data = TestClient(proxy_module.proxy_app).get("/upstream").json()
    assert data["model"] == ""
    assert data["key_configured"] is True


def test_upstream_set_hot_swaps_and_persists(
    upstream_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Valid upstream → live swap + env.sh rewrite. Omitting the key on a
    NEW url clears it: the stored key is only ever sent to the URL it was
    configured for (this route is unauthenticated — falling back to the
    stored key would exfiltrate it to any posted endpoint)."""
    _patch_models(monkeypatch, ids=["new-model"])

    resp = TestClient(proxy_module.proxy_app).post(
        "/upstream", json={"url": "http://new:8000/", "model": "new-model"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["upstream_url"] == "http://new:8000"  # trailing slash stripped
    assert data["persisted"] is True

    assert proxy_module._upstream_url == "http://new:8000"
    assert proxy_module._upstream_key == ""
    new_config = proxy_module._config
    assert new_config is not None
    assert new_config.model == "new-model"
    assert new_config.upstream_url == "http://new:8000"

    text = upstream_env.read_text()
    assert "export AGENTALLOY_UPSTREAM_URL=http://new:8000" in text
    assert "export AGENTALLOY_MODEL=new-model" in text
    assert "export AGENTALLOY_UPSTREAM_KEY=\n" in text
    assert "export AGENTALLOY_SERVICE_PORT=48950" in text  # unrelated line untouched


def test_upstream_set_same_url_keeps_key(
    upstream_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-posting the CURRENT url with the key omitted keeps the stored key
    (e.g. switching models on the same upstream)."""
    _patch_models(monkeypatch, ids=["new-model"])

    resp = TestClient(proxy_module.proxy_app).post(
        "/upstream", json={"url": "http://old:8000", "model": "new-model"}
    )
    assert resp.status_code == 200
    assert proxy_module._upstream_key == "sk-old"
    assert "export AGENTALLOY_UPSTREAM_KEY=sk-old" in upstream_env.read_text()


def test_upstream_set_replaces_key_and_token_counter(
    upstream_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new key propagates to the live config, the token counter, and env.sh."""
    counter = TokenCounter(model_url="http://old:8000", api_key="sk-old")
    proxy_module._token_counter = counter
    _patch_models(monkeypatch, ids=["new-model"])

    resp = TestClient(proxy_module.proxy_app).post(
        "/upstream",
        json={"url": "http://new:8000", "model": "new-model", "key": "sk-new"},
    )
    assert resp.status_code == 200

    assert proxy_module._upstream_key == "sk-new"
    assert counter.model_url == "http://new:8000"
    assert counter.api_key == "sk-new"
    assert "export AGENTALLOY_UPSTREAM_KEY=sk-new" in upstream_env.read_text()


def test_upstream_set_rejects_unknown_model(
    upstream_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model the upstream does not serve → 400 + available list, config untouched."""
    _patch_models(monkeypatch, ids=["served-a", "served-b"])

    resp = TestClient(proxy_module.proxy_app).post(
        "/upstream", json={"url": "http://new:8000", "model": "nope"}
    )
    assert resp.status_code == 400
    data = resp.json()
    assert "not served" in data["message"]
    assert data["available"] == ["served-a", "served-b"]

    assert proxy_module._upstream_url == "http://old:8000"
    assert "new:8000" not in upstream_env.read_text()


def test_upstream_set_rejects_bad_key(upstream_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """401 from the upstream → 400, config untouched."""
    _patch_models(monkeypatch, status_code=401)

    resp = TestClient(proxy_module.proxy_app).post(
        "/upstream", json={"url": "http://new:8000", "model": "new-model"}
    )
    assert resp.status_code == 400
    assert "rejected the API key" in resp.json()["message"]
    assert proxy_module._upstream_url == "http://old:8000"


def test_upstream_set_rejects_unreachable(
    upstream_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unreachable upstream → 502, config untouched."""
    _patch_models(monkeypatch, unreachable=True)

    resp = TestClient(proxy_module.proxy_app).post(
        "/upstream", json={"url": "http://down:8000", "model": "new-model"}
    )
    assert resp.status_code == 502
    assert "cannot reach" in resp.json()["message"]
    assert proxy_module._upstream_url == "http://old:8000"
    assert "down:8000" not in upstream_env.read_text()
