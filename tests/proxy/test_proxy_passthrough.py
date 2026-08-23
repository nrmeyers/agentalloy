"""Proxy router — basic passthrough and streaming tests.

Tests the /v1/chat/completions endpoint with mock upstream responses
using httpx.MockTransport / httpx.MockTransport for async clients.
"""

from __future__ import annotations

import json
import unittest.mock as mock
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from agentalloy.api import proxy_passthrough_router, proxy_router
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
                    "metadata": {"cwd": "/home/user/project", "sessionId": "s-1"},
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
        # "cwd" is the proxy's repo-resolution channel and is stripped before
        # forwarding; other metadata keys pass through.
        assert "cwd" not in received_request["metadata"]
        assert received_request["metadata"]["sessionId"] == "s-1"

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

    def test_upstream_4xx_forwards_body_and_records_error(self, app_with_upstream: Any) -> None:
        """Upstream 4xx forwards the upstream's own status + body to the caller
        and records an llm_error (not llm_received) in telemetry (1.3 regression).

        The old code fell through to the success telemetry path for 4xx,
        recording a false llm_received. The fix routes 4xx through
        _emit_llm_error and forwards the real status + body (5xx still maps to
        a generic 503).
        """
        error_body = {"error": {"message": "rate limited", "type": "rate_limit"}}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json=error_body, request=request)

        app_with_upstream.state.upstream_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://mock-upstream/v1",
        )

        with (
            mock.patch.object(proxy_router, "_emit_llm_error") as m_err,
            mock.patch.object(proxy_router, "_emit_llm_received") as m_recv,
            TestClient(app_with_upstream) as client,
        ):
            resp = client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4",
                    "messages": [
                        {"role": "user", "content": "Hello"},
                    ],
                },
            )

        # 4xx is forwarded with its real status + body (not a generic 503).
        assert resp.status_code == 429
        assert resp.json() == error_body
        # Telemetry recorded an error, not a success.
        m_err.assert_called_once()
        m_recv.assert_not_called()

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


# ---------------------------------------------------------------------------
# Surface-aware terminal-event guard tests (1.1)
# ---------------------------------------------------------------------------


class TestChunkHasTerminalEvent:
    """Unit tests for the surface-aware _chunk_has_terminal_event helper (1.1).

    The old _chunk_has_finish_reason keyed on the OpenAI chat.completions
    ``finish_reason`` shape, which never appears on the Anthropic Messages or
    OpenAI Responses surfaces — so it always reported "no terminal event" and
    the relay appended a mismatched corrective chunk to every stream. The fix
    scans for the surface's own SSE terminal event instead.
    """

    def test_anthropic_message_stop_detected(self) -> None:
        chunk = 'event: message_stop\ndata: {"type":"message_stop"}\n\n'
        assert proxy_passthrough_router._chunk_has_terminal_event(chunk, "message_stop") is True

    def test_responses_completed_detected(self) -> None:
        chunk = 'event: response.completed\ndata: {"type":"response.completed"}\n\n'
        assert (
            proxy_passthrough_router._chunk_has_terminal_event(chunk, "response.completed") is True
        )

    def test_non_terminal_event_not_detected(self) -> None:
        chunk = 'event: message_start\ndata: {"type":"message_start"}\n\n'
        assert proxy_passthrough_router._chunk_has_terminal_event(chunk, "message_stop") is False

    def test_openai_finish_reason_is_not_a_terminal_marker(self) -> None:
        # The old detector's shape must NOT count as a terminal event for the
        # Anthropic surface — that mismatch was the bug.
        chunk = 'data: {"id":"c1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
        assert proxy_passthrough_router._chunk_has_terminal_event(chunk, "message_stop") is False

    def test_empty_string(self) -> None:
        assert proxy_passthrough_router._chunk_has_terminal_event("", "message_stop") is False

    def test_multi_chunk_with_terminal_event(self) -> None:
        chunk = (
            'event: message_start\ndata: {"type":"message_start"}\n\n'
            'event: message_stop\ndata: {"type":"message_stop"}\n\n'
        )
        assert proxy_passthrough_router._chunk_has_terminal_event(chunk, "message_stop") is True


class TestProxyStreaming:
    """Tests for surface-aware terminal-event injection in streaming paths (1.1)."""

    def _make_mock_client(self, mock_response: mock.MagicMock) -> mock.MagicMock:
        """Build a mock AnthropicPassthroughClient with a stream context manager."""
        stream_cm = mock.MagicMock()
        stream_cm.__aenter__ = mock.AsyncMock(return_value=mock_response)
        stream_cm.__aexit__ = mock.AsyncMock()
        client = mock.MagicMock()
        client.stream.return_value = stream_cm
        return client

    async def _collect_body(self, resp) -> str:
        """Collect StreamingResponse body into a single string."""
        parts: list[bytes] = []
        async for chunk in resp.body_iterator:
            parts.append(chunk)
        return b"".join(parts).decode()

    async def test_truncated_stream_injects_surface_terminal_event(self) -> None:
        """The surface's terminal event is injected when the stream is truncated."""
        sse_data = 'event: message_start\ndata: {"type":"message_start"}\n\n'
        correction = 'event: message_stop\ndata: {"type":"message_stop"}\n\n'

        async def _aiter_raw():
            yield sse_data.encode()

        mock_response = mock.MagicMock()
        mock_response.status_code = 200
        mock_response.aiter_raw.return_value = _aiter_raw()
        mock_response.headers = {"content-type": "text/event-stream"}

        resp = await proxy_passthrough_router._forward_streaming(
            self._make_mock_client(mock_response),
            "test",
            {},
            sse_data.encode(),
        )
        body = await self._collect_body(resp)
        assert sse_data in body
        assert correction in body

    async def test_empty_stream_injects_surface_terminal_event(self) -> None:
        """The terminal event is injected for an empty (fully truncated) stream."""
        correction = 'event: message_stop\ndata: {"type":"message_stop"}\n\n'

        async def _aiter_raw():
            return
            yield  # unreachable — makes this an async generator

        mock_response = mock.MagicMock()
        mock_response.status_code = 200
        mock_response.aiter_raw.return_value = _aiter_raw()
        mock_response.headers = {"content-type": "text/event-stream"}

        resp = await proxy_passthrough_router._forward_streaming(
            self._make_mock_client(mock_response),
            "test",
            {},
            b"",
        )
        body = await self._collect_body(resp)
        assert correction in body

    async def test_complete_stream_not_duplicated(self) -> None:
        """Exactly one terminal event when upstream already carries it."""
        sse_data = 'event: message_stop\ndata: {"type":"message_stop"}\n\n'

        async def _aiter_raw():
            yield sse_data.encode()

        mock_response = mock.MagicMock()
        mock_response.status_code = 200
        mock_response.aiter_raw.return_value = _aiter_raw()
        mock_response.headers = {"content-type": "text/event-stream"}

        resp = await proxy_passthrough_router._forward_streaming(
            self._make_mock_client(mock_response),
            "test",
            {},
            sse_data.encode(),
        )
        body = await self._collect_body(resp)
        assert body == sse_data

    async def test_responses_surface_injects_its_own_terminal_event(self) -> None:
        """The /v1/responses surface injects response.completed, not message_stop."""
        sse_data = 'event: response.created\ndata: {"type":"response.created"}\n\n'
        correction = 'event: response.completed\ndata: {"type":"response.completed"}\n\n'

        async def _aiter_raw():
            yield sse_data.encode()

        mock_response = mock.MagicMock()
        mock_response.status_code = 200
        mock_response.aiter_raw.return_value = _aiter_raw()
        mock_response.headers = {"content-type": "text/event-stream"}

        resp = await proxy_passthrough_router._forward_streaming(
            self._make_mock_client(mock_response),
            "test",
            {},
            sse_data.encode(),
            path="/v1/responses",
        )
        body = await self._collect_body(resp)
        assert sse_data in body
        assert correction in body
        # The Anthropic terminal event must NOT leak into the Responses surface.
        assert "message_stop" not in body


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


class TestArtifactExtractionResponse:
    """Tests for artifact extraction on the response path (1.5)."""

    async def test_extracted_text_lands_in_first_block_rest_blanked(self) -> None:
        """When artifact extraction fires, the cleaned text goes into the FIRST
        text block and all other text blocks are blanked (1.5 regression).

        The cleaned text is the join of ALL text blocks with markers stripped,
        so it can't be split back per block (a marker may span blocks). The fix
        puts the full cleaned text in the first block and blanks the rest to
        avoid duplicating content or leaving unstripped markers behind.
        """
        from agentalloy.api.proxy_passthrough_router import _ArtifactExtractionContext

        upstream_content = json.dumps(
            {
                "content": [
                    {"type": "text", "text": "before <!-- artifact -->"},
                    {"type": "text", "text": "middle <!-- artifact --> after"},
                ]
            }
        ).encode()

        upstream = mock.MagicMock()
        upstream.status_code = 200
        upstream.content = upstream_content
        upstream.headers = {"content-type": "application/json"}

        client = mock.MagicMock()
        client.forward = mock.AsyncMock(return_value=upstream)

        ctx = _ArtifactExtractionContext(store=mock.MagicMock(), phase="design", slug="my-task")

        fake_result = mock.MagicMock()
        fake_result.extracted = True
        fake_result.cleaned_text = "CLEANED-TEXT"

        with mock.patch.object(
            proxy_passthrough_router, "extract_and_store", return_value=fake_result
        ):
            resp = await proxy_passthrough_router._forward_once(
                client, "test", {}, b"{}", artifact_extraction=ctx
            )

        body = json.loads(resp.body)
        text_blocks = [b for b in body["content"] if b.get("type") == "text"]
        assert len(text_blocks) == 2
        assert text_blocks[0]["text"] == "CLEANED-TEXT"
        assert text_blocks[1]["text"] == ""


class TestArtifactContextStoreScoping:
    """The artifact context must resolve the *scoped* state store.

    Regression for the corpus-store bug: the context used to hand extraction a
    store with no ``set_artifact`` (or the unscoped process store), so every
    extracted artifact soft-failed. The store must be the same ``for_repo``
    bucket the state leg reads from — a write through it must be visible via
    ``scoped_state_store(process_store(), root)`` and invisible to the unscoped
    process store.
    """

    @staticmethod
    def _signal(phase: str = "design", contract: str | None = "my-task") -> Any:
        sig = mock.MagicMock()
        sig.phase = phase
        sig.current_contract = contract
        return sig

    @staticmethod
    def _assert_scoped(ctx: Any, root: Path) -> None:
        from agentalloy.api.state_router import scoped_state_store
        from agentalloy.storage.state_store import process_store

        assert ctx is not None
        assert ctx.store is not None

        # Write through the context's store, then read it back via the SAME
        # scoped bucket the state leg uses.
        ctx.store.set_artifact("design", "my-task", "spec.md", "# scoped write\n")

        scoped = scoped_state_store(process_store(), root)
        scoped_rows = scoped.list_artifacts("design", slug="my-task")
        assert any(r.get("name") == "spec.artifact" for r in scoped_rows)

        # And it must NOT leak into the unscoped (default-bucket) process store.
        unscoped_rows = process_store().list_artifacts("design", slug="my-task")
        assert not any(r.get("name") == "spec.artifact" for r in unscoped_rows)

    def test_passthrough_context_scoped_to_project(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from agentalloy.api.proxy_passthrough_router import _build_artifact_context

        monkeypatch.setenv("ARTIFACT_EXTRACTION_ENABLED", "true")
        root = tmp_path / "repo"
        root.mkdir()

        ctx = _build_artifact_context(self._signal(), project_dir=root)
        self._assert_scoped(ctx, root)

    def test_openai_context_scoped_to_project(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from agentalloy.api.proxy_router import _build_openai_artifact_context

        monkeypatch.setenv("ARTIFACT_EXTRACTION_ENABLED", "true")
        root = tmp_path / "repo"
        root.mkdir()

        ctx = _build_openai_artifact_context(self._signal(), project_dir=root)
        self._assert_scoped(ctx, root)

    def test_passthrough_context_none_when_flag_off(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from agentalloy.api.proxy_passthrough_router import _build_artifact_context

        monkeypatch.setenv("ARTIFACT_EXTRACTION_ENABLED", "false")
        root = tmp_path / "repo"
        root.mkdir()

        assert _build_artifact_context(self._signal(), project_dir=root) is None
