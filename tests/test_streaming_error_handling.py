"""Tests for streaming/upstream error handling (Pattern D fix).

Verifies that streaming endpoints and upstream httpx calls properly handle
exceptions, returning structured error responses instead of raw 500s.

Covers:
- OpenAI-compatible proxy: streaming error on connection failure
- OpenAI-compatible proxy: streaming error on timeout
- OpenAI-compatible proxy: streaming error on HTTP error
- OpenAI-compatible proxy: non-streaming error on connection failure
- OpenAI-compatible proxy: non-streaming error on timeout
- OpenAI-compatible proxy: non-streaming error on HTTP error
- Anthropic proxy: streaming error on connection failure
- Anthropic proxy: streaming error on timeout
- Anthropic proxy: streaming error on HTTP error
- Anthropic proxy: non-streaming error on connection failure
- Anthropic proxy: non-streaming error on timeout
- Anthropic proxy: non-streaming error on HTTP error
- Embeddings endpoint: error handling
- error_sse helper functions
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from fastapi.testclient import TestClient

from agentalloy.api.upstream.error_sse import (
    error_sse_event,
    error_sse_plain,
    make_http_error_sse,
    make_network_error_sse,
)
from agentalloy.app import create_app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_upstream(
    response_body: dict[str, Any] | None = None,
    status_code: int = 200,
    stream_chunks: list[str] | None = None,
    raise_on_request: Exception | None = None,
) -> httpx.AsyncClient:
    """Create an httpx.AsyncClient with MockTransport for the upstream LLM."""

    def handler(request: httpx.Request) -> httpx.Response:
        if raise_on_request is not None:
            raise raise_on_request
        if stream_chunks is not None:
            return httpx.Response(
                status_code=status_code,
                content="".join(stream_chunks),
                headers={"content-type": "text/event-stream"},
                request=request,
            )
        if response_body is not None:
            return httpx.Response(
                status_code=status_code,
                json=response_body if status_code == 200 else {"error": str(response_body)},
                request=request,
            )
        return httpx.Response(status_code=status_code, request=request)

    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport, base_url="http://mock-upstream/v1")


def _make_mock_embed(
    status_code: int = 200,
    response_body: dict[str, Any] | None = None,
    raise_on_request: Exception | None = None,
) -> httpx.AsyncClient:
    """Create an httpx.AsyncClient with MockTransport for the embed server."""

    def handler(request: httpx.Request) -> httpx.Response:
        if raise_on_request is not None:
            raise raise_on_request
        if response_body is not None:
            return httpx.Response(
                status_code=status_code,
                json=response_body,
                request=request,
            )
        return httpx.Response(status_code=status_code, request=request)

    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport, base_url="http://mock-embed/v1")


# ---------------------------------------------------------------------------
# error_sse helper tests
# ---------------------------------------------------------------------------


class TestErrorSSEHelpers:
    """Test the error SSE helper functions."""

    def test_error_sse_plain_basic(self) -> None:
        result = error_sse_plain("test error")
        assert result.startswith("data: {")
        data = json.loads(result.strip().replace("data: ", "", 1))
        assert data["error"] == "test error"

    def test_error_sse_plain_with_status(self) -> None:
        result = error_sse_plain("server error", status_code=500)
        data = json.loads(result.strip().replace("data: ", "", 1))
        assert data["error"] == "server error"
        assert data["status_code"] == 500

    def test_error_sse_event_basic(self) -> None:
        result = error_sse_event("error", {"type": "api_error", "message": "fail"})
        assert result.startswith("event: error\n")
        lines = result.strip().split("\n")
        assert lines[0] == "event: error"
        data = json.loads(lines[1].replace("data: ", "", 1))
        assert data == {"type": "api_error", "message": "fail"}

    def test_make_http_error_sse(self) -> None:
        events = make_http_error_sse(500, "internal error")
        assert len(events) == 3
        # message_start
        assert "event: message_start" in events[0]
        # error
        assert "event: error" in events[1]
        error_data = json.loads(events[1].split("\n")[1].replace("data: ", "", 1))
        assert "500" in error_data["error"]["message"]
        # message_stop
        assert "event: message_stop" in events[2]

    def test_make_network_error_sse(self) -> None:
        exc = httpx.ConnectError("connection refused")
        events = make_network_error_sse(exc, model="gpt-4")
        assert len(events) == 3
        error_data = json.loads(events[1].split("\n")[1].replace("data: ", "", 1))
        assert "ConnectError" in error_data["error"]["message"]
        assert "connection refused" in error_data["error"]["message"]


# ---------------------------------------------------------------------------
# OpenAI-compatible proxy: streaming error handling
# ---------------------------------------------------------------------------


class TestOpenAIProxyStreamingErrors:
    """Test streaming error handling in the OpenAI-compatible proxy."""

    def test_streaming_connection_error(self) -> None:
        """Upstream connection error during streaming returns error SSE."""
        app = create_app(use_default_lifespan=False)
        app.state.upstream_client = _make_mock_upstream(
            raise_on_request=httpx.ConnectError("connection refused")
        )

        with (
            TestClient(app) as client,
            client.stream(
                "POST",
                "/v1/chat/completions",
                json={
                    "model": "gpt-4",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "stream": True,
                },
            ) as resp,
        ):
            assert resp.status_code == 200
            body = resp.read().decode()
            assert "error" in body.lower()
            assert "connection" in body.lower() or "refused" in body.lower()

    def test_streaming_timeout_error(self) -> None:
        """Upstream timeout during streaming returns error SSE."""
        app = create_app(use_default_lifespan=False)
        app.state.upstream_client = _make_mock_upstream(
            raise_on_request=httpx.TimeoutException("request timed out")
        )

        with (
            TestClient(app) as client,
            client.stream(
                "POST",
                "/v1/chat/completions",
                json={
                    "model": "gpt-4",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "stream": True,
                },
            ) as resp,
        ):
            assert resp.status_code == 200
            body = resp.read().decode()
            assert "error" in body.lower()
            assert "timeout" in body.lower()

    def test_streaming_http_error(self) -> None:
        """Upstream HTTP error during streaming returns error SSE."""
        app = create_app(use_default_lifespan=False)
        app.state.upstream_client = _make_mock_upstream(
            raise_on_request=httpx.ReadTimeout("read timed out")
        )

        with (
            TestClient(app) as client,
            client.stream(
                "POST",
                "/v1/chat/completions",
                json={
                    "model": "gpt-4",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "stream": True,
                },
            ) as resp,
        ):
            assert resp.status_code == 200
            body = resp.read().decode()
            assert "error" in body.lower()

    def test_streaming_upstream_500(self) -> None:
        """Upstream 500 during streaming returns error SSE chunk."""
        app = create_app(use_default_lifespan=False)
        app.state.upstream_client = _make_mock_upstream(
            status_code=500, response_body={"error": "Internal Server Error"}
        )

        with (
            TestClient(app) as client,
            client.stream(
                "POST",
                "/v1/chat/completions",
                json={
                    "model": "gpt-4",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "stream": True,
                },
            ) as resp,
        ):
            assert resp.status_code == 200
            body = resp.read().decode()
            assert "error" in body.lower()
            assert "500" in body


# ---------------------------------------------------------------------------
# OpenAI-compatible proxy: non-streaming error handling
# ---------------------------------------------------------------------------


class TestOpenAIProxyNonStreamingErrors:
    """Test non-streaming error handling in the OpenAI-compatible proxy."""

    def test_non_streaming_connection_error(self) -> None:
        """Upstream connection error returns 503 with error body."""
        app = create_app(use_default_lifespan=False)
        app.state.upstream_client = _make_mock_upstream(
            raise_on_request=httpx.ConnectError("connection refused")
        )

        with TestClient(app) as client:
            resp = client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            )

        assert resp.status_code == 503
        data = resp.json()
        assert "error" in data
        assert "connection" in data["error"].get("message", "").lower()

    def test_non_streaming_timeout_error(self) -> None:
        """Upstream timeout returns 503 with error body."""
        app = create_app(use_default_lifespan=False)
        app.state.upstream_client = _make_mock_upstream(
            raise_on_request=httpx.TimeoutException("request timed out")
        )

        with TestClient(app) as client:
            resp = client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            )

        assert resp.status_code == 503
        data = resp.json()
        assert "error" in data
        assert "timed out" in data["error"].get("message", "").lower()

    def test_non_streaming_http_error(self) -> None:
        """Upstream HTTP error returns 503 with error body."""
        app = create_app(use_default_lifespan=False)
        app.state.upstream_client = _make_mock_upstream(
            raise_on_request=httpx.ReadTimeout("read timed out")
        )

        with TestClient(app) as client:
            resp = client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            )

        assert resp.status_code == 503
        data = resp.json()
        assert "error" in data


# ---------------------------------------------------------------------------
# Anthropic proxy: streaming error handling
# ---------------------------------------------------------------------------


class TestAnthropicProxyStreamingErrors:
    """Test streaming error handling in the Anthropic proxy."""

    def test_streaming_connection_error(self) -> None:
        """Upstream connection error during Anthropic streaming returns error SSE."""
        app = create_app(use_default_lifespan=False)
        app.state.upstream_client = _make_mock_upstream(
            raise_on_request=httpx.ConnectError("connection refused")
        )

        with (
            TestClient(app) as client,
            client.stream(
                "POST",
                "/v1/messages",
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 1024,
                    "messages": [{"role": "user", "content": "Hello"}],
                    "stream": True,
                },
            ) as resp,
        ):
            assert resp.status_code == 200
            body = resp.read().decode()
            # Should contain structured error SSE events
            assert "event: message_start" in body
            assert "event: error" in body
            assert "connection" in body.lower() or "refused" in body.lower()
            # Should end with message_stop
            assert "event: message_stop" in body

    def test_streaming_timeout_error(self) -> None:
        """Upstream timeout during Anthropic streaming returns error SSE."""
        app = create_app(use_default_lifespan=False)
        app.state.upstream_client = _make_mock_upstream(
            raise_on_request=httpx.TimeoutException("request timed out")
        )

        with (
            TestClient(app) as client,
            client.stream(
                "POST",
                "/v1/messages",
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 1024,
                    "messages": [{"role": "user", "content": "Hello"}],
                    "stream": True,
                },
            ) as resp,
        ):
            assert resp.status_code == 200
            body = resp.read().decode()
            assert "event: message_start" in body
            assert "event: error" in body
            assert "timeout" in body.lower()
            assert "event: message_stop" in body

    def test_streaming_http_error(self) -> None:
        """Upstream HTTP error during Anthropic streaming returns error SSE."""
        app = create_app(use_default_lifespan=False)
        app.state.upstream_client = _make_mock_upstream(
            raise_on_request=httpx.ReadTimeout("read timed out")
        )

        with (
            TestClient(app) as client,
            client.stream(
                "POST",
                "/v1/messages",
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 1024,
                    "messages": [{"role": "user", "content": "Hello"}],
                    "stream": True,
                },
            ) as resp,
        ):
            assert resp.status_code == 200
            body = resp.read().decode()
            assert "event: message_start" in body
            assert "event: error" in body
            assert "event: message_stop" in body

    def test_streaming_upstream_500(self) -> None:
        """Upstream 500 during Anthropic streaming returns error SSE."""
        app = create_app(use_default_lifespan=False)
        app.state.upstream_client = _make_mock_upstream(
            status_code=500, response_body={"error": "Internal Server Error"}
        )

        with (
            TestClient(app) as client,
            client.stream(
                "POST",
                "/v1/messages",
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 1024,
                    "messages": [{"role": "user", "content": "Hello"}],
                    "stream": True,
                },
            ) as resp,
        ):
            assert resp.status_code == 200
            body = resp.read().decode()
            assert "event: message_start" in body
            assert "event: error" in body
            assert "500" in body
            assert "event: message_stop" in body


# ---------------------------------------------------------------------------
# Anthropic proxy: non-streaming error handling
# ---------------------------------------------------------------------------


class TestAnthropicProxyNonStreamingErrors:
    """Test non-streaming error handling in the Anthropic proxy."""

    def test_non_streaming_connection_error(self) -> None:
        """Upstream connection error returns 503 with error body."""
        app = create_app(use_default_lifespan=False)
        app.state.upstream_client = _make_mock_upstream(
            raise_on_request=httpx.ConnectError("connection refused")
        )

        with TestClient(app) as client:
            resp = client.post(
                "/v1/messages",
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 1024,
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            )

        assert resp.status_code == 503
        data = resp.json()
        assert "error" in data
        assert "connection" in data["error"].get("message", "").lower()

    def test_non_streaming_timeout_error(self) -> None:
        """Upstream timeout returns 503 with error body."""
        app = create_app(use_default_lifespan=False)
        app.state.upstream_client = _make_mock_upstream(
            raise_on_request=httpx.TimeoutException("request timed out")
        )

        with TestClient(app) as client:
            resp = client.post(
                "/v1/messages",
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 1024,
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            )

        assert resp.status_code == 503
        data = resp.json()
        assert "error" in data
        assert "timed out" in data["error"].get("message", "").lower()

    def test_non_streaming_http_error(self) -> None:
        """Upstream HTTP error returns 503 with error body."""
        app = create_app(use_default_lifespan=False)
        app.state.upstream_client = _make_mock_upstream(
            raise_on_request=httpx.ReadTimeout("read timed out")
        )

        with TestClient(app) as client:
            resp = client.post(
                "/v1/messages",
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 1024,
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            )

        assert resp.status_code == 503
        data = resp.json()
        assert "error" in data


# ---------------------------------------------------------------------------
# Embeddings endpoint error handling
# ---------------------------------------------------------------------------


class TestEmbeddingsErrorHandling:
    """Test error handling in the /v1/embeddings endpoint."""

    def test_embeddings_connection_error(self) -> None:
        """Embed server connection error returns 503."""
        app = create_app(use_default_lifespan=False)
        app.state.embed_async_client = _make_mock_embed(
            raise_on_request=httpx.ConnectError("connection refused")
        )

        with TestClient(app) as client:
            resp = client.post(
                "/v1/embeddings",
                json={"model": "text-embedding-ada-002", "input": "Hello"},
            )

        assert resp.status_code == 503
        data = resp.json()
        assert "error" in data
        assert "connection" in data["error"].get("message", "").lower()

    def test_embeddings_timeout_error(self) -> None:
        """Embed server timeout returns 503."""
        app = create_app(use_default_lifespan=False)
        app.state.embed_async_client = _make_mock_embed(
            raise_on_request=httpx.TimeoutException("request timed out")
        )

        with TestClient(app) as client:
            resp = client.post(
                "/v1/embeddings",
                json={"model": "text-embedding-ada-002", "input": "Hello"},
            )

        assert resp.status_code == 503
        data = resp.json()
        assert "error" in data
        assert "timed out" in data["error"].get("message", "").lower()

    def test_embeddings_http_error(self) -> None:
        """Embed server HTTP error returns 503."""
        app = create_app(use_default_lifespan=False)
        app.state.embed_async_client = _make_mock_embed(
            raise_on_request=httpx.ReadTimeout("read timed out")
        )

        with TestClient(app) as client:
            resp = client.post(
                "/v1/embeddings",
                json={"model": "text-embedding-ada-002", "input": "Hello"},
            )

        assert resp.status_code == 503
        data = resp.json()
        assert "error" in data
