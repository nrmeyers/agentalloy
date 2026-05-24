"""Proxy router — basic passthrough tests.

Tests the /v1/chat/completions endpoint with mock upstream responses
using httpx.MockTransport.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from agentalloy.app import create_app


def _make_mock_upstream(response_body: dict[str, Any], status_code: int = 200) -> httpx.Client:
    """Create an httpx.Client with MockTransport for the upstream LLM."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=status_code,
            json=response_body,
            request=request,
        )

    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport, base_url="http://mock-upstream/v1")


class TestProxyPassthrough:
    """Test the basic proxy passthrough endpoint."""

    @pytest.fixture
    def app_with_upstream(self):
        """Create an app with a mock upstream client."""
        app = create_app(use_default_lifespan=False)
        mock_client = _make_mock_upstream({})
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

        # Recreate the mock client with the actual response body
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=response_body, request=request)

        app_with_upstream.state.upstream_client = httpx.Client(
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

        app_with_upstream.state.upstream_client = httpx.Client(
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

        app_with_upstream.state.upstream_client = httpx.Client(
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

        app_with_upstream.state.upstream_client = httpx.Client(
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
