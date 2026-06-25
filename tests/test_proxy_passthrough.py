"""Proxy router — basic passthrough and streaming tests.

Tests the /v1/chat/completions endpoint with mock upstream responses
using httpx.MockTransport / httpx.MockTransport for async clients.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from agentalloy.app import create_app


def _make_mock_async_upstream(
    response_body: dict[str, Any],
    status_code: int = 200,
) -> httpx.AsyncClient:
    """Create an httpx.AsyncClient with MockTransport for the upstream LLM."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=status_code,
            json=response_body,
            request=request,
        )

    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport, base_url="http://mock-upstream/v1")


class TestProxyPassthrough:
    """Test the basic proxy passthrough endpoint."""

    @pytest.fixture
    def app_with_upstream(self):
        """Create an app with a mock upstream client."""
        app = create_app(use_default_lifespan=False)
        mock_client = _make_mock_async_upstream({})
        app.state.upstream_client = mock_client
        return app

    @pytest.fixture
    def app_no_upstream(self):
        """Create an app without an upstream client."""
        app = create_app(use_default_lifespan=False)
        app.state.upstream_client = None
        return app

    def test_basic_passthrough(self, app_with_upstream: Any) -> None:
        """Request is forwarded to upstream, response returned unchanged."""
        response_body = {
            "id": "chatcmpl-123",
            "object": "chat.completion",
            "created": 1234567890,
            "model": "gpt-4",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hello!"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=response_body, request=request)

        app_with_upstream.state.upstream_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://mock-upstream/v1",
        )

        with TestClient(app_with_upstream) as client:
            resp = client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4",
                    "messages": [
                        {"role": "user", "content": "Say hello"},
                    ],
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "chatcmpl-123"
        assert data["choices"][0]["message"]["content"] == "Hello!"
        assert data["usage"]["total_tokens"] == 15

    def test_upstream_not_configured(self, app_no_upstream: Any) -> None:
        """Returns 503 when no upstream client is configured."""
        with TestClient(app_no_upstream) as client:
            resp = client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4",
                    "messages": [
                        {"role": "user", "content": "Hello"},
                    ],
                },
            )

        assert resp.status_code == 503
        data = resp.json()
        assert data["error"]["code"] == "upstream_not_configured"

    def test_request_body_preserved(self, app_with_upstream: Any) -> None:
        """All fields in the request are forwarded to the upstream."""
        received_request: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            received_request.update(json.loads(request.content.decode()))
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "created": 1234567890,
                    "model": "gpt-4",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "OK"},
                            "finish_reason": "stop",
                        }
                    ],
                },
                request=request,
            )

        app_with_upstream.state.upstream_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://mock-upstream/v1",
        )

        with TestClient(app_with_upstream) as client:
            client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4",
                    "messages": [
                        {"role": "system", "content": "You are helpful"},
                        {"role": "user", "content": "Hello"},
                    ],
                    "temperature": 0.7,
                    "max_tokens": 100,
                    "top_p": 0.9,
                    "presence_penalty": 0.1,
                    "frequency_penalty": 0.1,
                    "n": 1,
                    "user": "user-123",
                    "metadata": {"cwd": "/home/user/project"},
                },
            )

        assert received_request["model"] == "gpt-4"
        assert len(received_request["messages"]) == 2
        assert received_request["messages"][0]["role"] == "system"
        assert received_request["messages"][1]["role"] == "user"
        assert received_request["temperature"] == 0.7
        assert received_request["max_tokens"] == 100
        assert received_request["top_p"] == 0.9
        assert received_request["presence_penalty"] == 0.1
        assert received_request["frequency_penalty"] == 0.1
        assert received_request["n"] == 1
        assert received_request["user"] == "user-123"
        assert received_request["metadata"]["cwd"] == "/home/user/project"

    def test_upstream_error_500(self, app_with_upstream: Any) -> None:
        """Upstream 5xx returns 503 to the client."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="Internal Server Error", request=request)

        app_with_upstream.state.upstream_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://mock-upstream/v1",
        )

        with TestClient(app_with_upstream) as client:
            resp = client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4",
                    "messages": [
                        {"role": "user", "content": "Hello"},
                    ],
                },
            )

        assert resp.status_code == 503
        data = resp.json()
        assert data["error"]["code"] == "upstream_unavailable"

    def test_stream_flag_forwarded(self, app_with_upstream: Any) -> None:
        """Stream flag is forwarded in the request payload."""
        received_stream: bool | None = None

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal received_stream
            body = json.loads(request.content.decode())
            received_stream = body.get("stream")
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "created": 1234567890,
                    "model": "gpt-4",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "OK"},
                            "finish_reason": "stop",
                        }
                    ],
                },
                request=request,
            )

        app_with_upstream.state.upstream_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://mock-upstream/v1",
        )

        with TestClient(app_with_upstream) as client:
            client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4",
                    "messages": [
                        {"role": "user", "content": "Hello"},
                    ],
                    "stream": True,
                },
            )

        assert received_stream is True


class TestProxyStreaming:
    """Test SSE streaming passthrough."""

    def test_stream_passthrough(self) -> None:
        """Streaming request returns SSE chunks forwarded from upstream."""
        chunks = [
            'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","created":123,"model":"gpt-4","choices":[{"index":0,"delta":{"role":"assistant","content":""}}]}\n\n',
            'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","created":123,"model":"gpt-4","choices":[{"index":0,"delta":{"content":"Hello"}}]}\n\n',
            'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","created":123,"model":"gpt-4","choices":[{"index":0,"delta":{"content":" world"}}]}\n\n',
            'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","created":123,"model":"gpt-4","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n',
            "data: [DONE]\n\n",
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content="".join(chunks),
                headers={"content-type": "text/event-stream"},
                request=request,
            )

        app = create_app(use_default_lifespan=False)
        app.state.upstream_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://mock-upstream/v1",
        )

        with (
            TestClient(app) as client,
            client.stream(
                "POST",
                "/v1/chat/completions",
                json={
                    "model": "gpt-4",
                    "messages": [
                        {"role": "user", "content": "Say hello"},
                    ],
                    "stream": True,
                },
            ) as resp,
        ):
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            body = resp.read().decode()
            assert "Hello" in body
            assert "world" in body
            assert "[DONE]" in body

    def test_stream_format_preserved(self) -> None:
        """SSE format is preserved — data: prefix and double newlines."""
        sse_data = 'data: {"id":"test","object":"chat.completion.chunk"}\n\n'

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=sse_data,
                headers={"content-type": "text/event-stream"},
                request=request,
            )

        app = create_app(use_default_lifespan=False)
        app.state.upstream_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://mock-upstream/v1",
        )

        with (
            TestClient(app) as client,
            client.stream(
                "POST",
                "/v1/chat/completions",
                json={
                    "model": "gpt-4",
                    "messages": [
                        {"role": "user", "content": "Hello"},
                    ],
                    "stream": True,
                },
            ) as resp,
        ):
            assert resp.status_code == 200
            assert resp.read().decode() == sse_data

    def test_stream_upstream_error_500(self) -> None:
        """Upstream 5xx during streaming returns error SSE chunk."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="Internal Server Error", request=request)

        app = create_app(use_default_lifespan=False)
        app.state.upstream_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://mock-upstream/v1",
        )

        with (
            TestClient(app) as client,
            client.stream(
                "POST",
                "/v1/chat/completions",
                json={
                    "model": "gpt-4",
                    "messages": [
                        {"role": "user", "content": "Hello"},
                    ],
                    "stream": True,
                },
            ) as resp,
        ):
            assert resp.status_code == 200  # Streaming response always 200
            body = resp.read().decode()
            assert "error" in body.lower()
            assert "500" in body


class TestEmbeddingsPassthrough:
    """Tests for /v1/embeddings passthrough."""

    def test_embeddings_passthrough(self) -> None:
        """Embeddings request is forwarded to embed server."""
        from agentalloy.app import create_app

        captured_request = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured_request["body"] = json.loads(request.content.decode())
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {
                            "object": "embedding",
                            "index": 0,
                            "embedding": [0.1, 0.2, 0.3],
                        }
                    ],
                    "model": "text-embedding-ada-002",
                    "usage": {"prompt_tokens": 8, "total_tokens": 8},
                },
                request=request,
            )

        app = create_app(use_default_lifespan=False)
        app.state.embed_async_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://mock-embed/v1",
        )

        with TestClient(app) as client:
            resp = client.post(
                "/v1/embeddings",
                json={
                    "model": "text-embedding-ada-002",
                    "input": "Hello world",
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["object"] == "list"
        assert len(body["data"]) == 1
        assert body["data"][0]["embedding"] == [0.1, 0.2, 0.3]

    def test_embeddings_no_embed_client(self) -> None:
        """When embed client is not configured, return 503."""
        from agentalloy.app import create_app

        app = create_app(use_default_lifespan=False)
        app.state.embed_client = None

        with TestClient(app) as client:
            resp = client.post(
                "/v1/embeddings",
                json={
                    "model": "text-embedding-ada-002",
                    "input": "Hello world",
                },
            )

        assert resp.status_code == 503
        body = resp.json()
        assert body["error"]["code"] == "embed_not_configured"


class TestAnthropicConverter:
    """`_proxy_request_from_anthropic` maps the fields the signal layer reads."""

    def test_maps_tools_for_carrier_gate(self) -> None:
        # The carrier-request gate keys on ProxyRequest.tools; the converter must
        # surface the Anthropic top-level `tools` array so a real agent turn is
        # distinguishable from a tool-less background micro-request.
        from agentalloy.api.proxy_passthrough_router import _proxy_request_from_anthropic

        payload: dict[str, Any] = {
            "model": "claude-opus-4-8",
            "messages": [{"role": "user", "content": "do the thing"}],
            "tools": [{"name": "Read", "description": "read", "input_schema": {}}],
        }
        req = _proxy_request_from_anthropic(payload)
        assert req.tools is not None and len(req.tools) == 1
        assert bool(req.tools) is True  # carrier

    def test_no_tools_yields_none(self) -> None:
        from agentalloy.api.proxy_passthrough_router import _proxy_request_from_anthropic

        payload: dict[str, Any] = {
            "model": "claude-haiku-4-5",
            "messages": [{"role": "user", "content": "quota"}],
        }
        req = _proxy_request_from_anthropic(payload)
        assert req.tools is None  # background request → not a carrier
