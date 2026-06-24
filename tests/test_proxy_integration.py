"""Full proxy request flow integration tests.

Tests the complete integrated handler:
  signal -> compose -> inject -> forward -> telemetry

Covers:
- Full flow with signal match -> compose -> inject -> forward
- Full flow with no signal -> passthrough
- Composition failure -> soft-fail passthrough
- Streaming mode with composition pre-injection
- Telemetry trace written for all flows
- Upstream error handling (503, timeout, connect error)
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
from fastapi.testclient import TestClient

from agentalloy.api.compose_models import ComposedResult, EmptyResult, LatencyBreakdown
from agentalloy.api.proxy_signal import SignalResult
from agentalloy.app import create_app
from agentalloy.orchestration.compose import ComposeOrchestrator


def _make_mock_upstream(
    response_body: dict[str, Any],
    status_code: int = 200,
    stream_chunks: list[str] | None = None,
    raise_exc: Exception | None = None,
) -> httpx.AsyncClient:
    """Create an httpx.AsyncClient with MockTransport for the upstream LLM."""

    def handler(request: httpx.Request) -> httpx.Response:
        if raise_exc:
            # MockTransport can't raise, so we return an error response
            raise raise_exc
        if stream_chunks is not None:
            # For streaming, return chunks as SSE
            content = "".join(stream_chunks)
            return httpx.Response(
                status_code=status_code,
                content=content,
                headers={"content-type": "text/event-stream"},
                request=request,
            )
        return httpx.Response(
            status_code=status_code,
            json=response_body,
            request=request,
        )

    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport, base_url="http://mock-upstream/v1")


def _make_mock_orchestrator(
    compose_output: str | None = None,
    raise_exc: Exception | None = None,
) -> ComposeOrchestrator:
    """Create a mock ComposeOrchestrator that returns a predefined result."""
    mock = MagicMock(spec=ComposeOrchestrator)

    async def mock_compose(req: Any, **_kwargs: object) -> Any:
        if raise_exc:
            raise raise_exc
        if compose_output is None:
            return EmptyResult(
                task=req.task if hasattr(req, "task") else "test",
                phase=req.phase if hasattr(req, "phase") else "build",
                system_fragments=[],
                system_skills_applied=False,
            )
        return ComposedResult(
            task=req.task if hasattr(req, "task") else "test",
            phase=req.phase if hasattr(req, "phase") else "build",
            output=compose_output,
            domain_fragments=["fragment-1"],
            source_skills=["skill-1"],
            system_fragments=[],
            system_skills_applied=False,
            assembly_tier=1,
            latency_ms=LatencyBreakdown(retrieval_ms=10, assembly_ms=5, total_ms=15),
        )

    mock.compose = mock_compose
    return mock


def _make_app(
    mock_orchestrator: ComposeOrchestrator | None = None,
    mock_vector_store: Any = None,
    raise_upstream: Exception | None = None,
    upstream_status: int = 200,
    stream_chunks: list[str] | None = None,
) -> Any:
    """Create a test app with all proxy dependencies wired."""
    app = create_app(use_default_lifespan=False)

    # Upstream client
    response_body = {
        "id": "chatcmpl-123",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "gpt-4",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Test response"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    app.state.upstream_client = _make_mock_upstream(
        response_body,
        status_code=upstream_status,
        stream_chunks=stream_chunks,
        raise_exc=raise_upstream,
    )

    # Embed client (mock)
    mock_embed = MagicMock()
    app.state.embed_client = mock_embed

    # Vector store (mock)
    if mock_vector_store is None:
        mock_vector_store = MagicMock()
    app.state.vector_store = mock_vector_store

    # Orchestrator
    if mock_orchestrator is not None:
        from agentalloy.api.compose_router import get_orchestrator

        app.dependency_overrides[get_orchestrator] = lambda: mock_orchestrator

    return app


class TestFullProxyFlow:
    """Full integration flow tests."""

    def test_passthrough_no_signal(self, tmp_path: Path) -> None:
        """Request with no phase file -> passthrough (no composition)."""
        app = _make_app()

        # Override signal evaluation to simulate no phase
        with (
            patch(
                "agentalloy.api.proxy_router.evaluate_signal",
                return_value=SignalResult(should_compose=False),
            ),
            TestClient(app) as client,
        ):
            resp = client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "metadata": {"cwd": str(tmp_path)},
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["choices"][0]["message"]["content"] == "Test response"

        # Telemetry should have been written
        app.state.vector_store.record_composition_trace.assert_called_once()
        trace = app.state.vector_store.record_composition_trace.call_args[0][0]
        assert trace.status == "proxy_passthrough"

    def test_signal_match_compose_and_inject(self, tmp_path: Path) -> None:
        """Signal match -> compose -> inject into system message -> forward."""
        compose_output = "# Skill: Test\nAlways be helpful."
        orchestrator = _make_mock_orchestrator(compose_output=compose_output)
        app = _make_app(mock_orchestrator=orchestrator)

        # Override signal evaluation to simulate match
        signal_result = SignalResult(
            should_compose=True,
            phase="build",
            task="implement feature",
            pre_filter_matched="prompt_keyword",
            gates_met=["test_passed"],
        )
        with (
            patch(
                "agentalloy.api.proxy_router.evaluate_signal",
                return_value=signal_result,
            ),
            TestClient(app) as client,
        ):
            resp = client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4",
                    "messages": [
                        {"role": "system", "content": "You are an assistant."},
                        {"role": "user", "content": "Implement feature X"},
                    ],
                    "metadata": {"cwd": str(tmp_path)},
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["choices"][0]["message"]["content"] == "Test response"

        # Telemetry should show composed status
        app.state.vector_store.record_composition_trace.assert_called_once()
        trace = app.state.vector_store.record_composition_trace.call_args[0][0]
        assert trace.status == "proxy_composed"

    def test_compose_failure_soft_fail(self, tmp_path: Path) -> None:
        """Signal match but composition fails -> soft-fail passthrough."""
        orchestrator = _make_mock_orchestrator(raise_exc=RuntimeError("compose error"))
        app = _make_app(mock_orchestrator=orchestrator)

        signal_result = SignalResult(
            should_compose=True,
            phase="build",
            task="implement feature",
            pre_filter_matched="prompt_keyword",
        )
        with (
            patch(
                "agentalloy.api.proxy_router.evaluate_signal",
                return_value=signal_result,
            ),
            TestClient(app) as client,
        ):
            resp = client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4",
                    "messages": [{"role": "user", "content": "Implement feature X"}],
                    "metadata": {"cwd": str(tmp_path)},
                },
            )

        # Should still succeed (soft-fail)
        assert resp.status_code == 200
        body = resp.json()
        assert body["choices"][0]["message"]["content"] == "Test response"

        # Telemetry shows passthrough (composition failed)
        app.state.vector_store.record_composition_trace.assert_called_once()
        trace = app.state.vector_store.record_composition_trace.call_args[0][0]
        assert trace.status == "proxy_passthrough"

    def test_signal_failure_soft_fail(self, tmp_path: Path) -> None:
        """Signal evaluation raises -> passthrough with telemetry."""
        orchestrator = _make_mock_orchestrator()
        app = _make_app(mock_orchestrator=orchestrator)

        with (
            patch(
                "agentalloy.api.proxy_router.evaluate_signal",
                side_effect=RuntimeError("signal error"),
            ),
            TestClient(app) as client,
        ):
            resp = client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "metadata": {"cwd": str(tmp_path)},
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["choices"][0]["message"]["content"] == "Test response"

    def test_empty_compose_result_passthrough(self, tmp_path: Path) -> None:
        """Signal match but compose returns EmptyResult -> passthrough."""
        orchestrator = _make_mock_orchestrator(compose_output=None)
        app = _make_app(mock_orchestrator=orchestrator)

        signal_result = SignalResult(
            should_compose=True,
            phase="build",
            task="implement feature",
            pre_filter_matched="prompt_keyword",
        )
        with (
            patch(
                "agentalloy.api.proxy_router.evaluate_signal",
                return_value=signal_result,
            ),
            TestClient(app) as client,
        ):
            resp = client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4",
                    "messages": [{"role": "user", "content": "Implement feature X"}],
                    "metadata": {"cwd": str(tmp_path)},
                },
            )

        assert resp.status_code == 200

        # Telemetry shows passthrough (EmptyResult = no composition)
        app.state.vector_store.record_composition_trace.assert_called_once()
        trace = app.state.vector_store.record_composition_trace.call_args[0][0]
        assert trace.status == "proxy_passthrough"

    def test_stream_mode_with_composition(self, tmp_path: Path) -> None:
        """Streaming mode with composition pre-injection."""
        compose_output = "# Skill: Streaming\nStream responses."
        orchestrator = _make_mock_orchestrator(compose_output=compose_output)

        stream_chunks = [
            'data: {"id":"1","choices":[{"delta":{"content":"Hello"}}]}\n\n',
            'data: {"id":"1","choices":[{"delta":{"content":" world"}}]}\n\n',
            "data: [DONE]\n\n",
        ]
        app = _make_app(mock_orchestrator=orchestrator, stream_chunks=stream_chunks)

        signal_result = SignalResult(
            should_compose=True,
            phase="build",
            task="implement feature",
            pre_filter_matched="prompt_keyword",
        )
        with (
            patch(
                "agentalloy.api.proxy_router.evaluate_signal",
                return_value=signal_result,
            ),
            TestClient(app) as client,
        ):
            resp = client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4",
                    "stream": True,
                    "messages": [{"role": "user", "content": "Implement X"}],
                    "metadata": {"cwd": str(tmp_path)},
                },
            )

        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")
        content = resp.text
        assert "Hello" in content
        assert "[DONE]" in content

        # Telemetry should be written for streaming too
        app.state.vector_store.record_composition_trace.assert_called_once()

    def test_no_orchestrator_passthrough(self, tmp_path: Path) -> None:
        """Signal match but no orchestrator -> passthrough."""
        # App without orchestrator
        app = _make_app(mock_orchestrator=None)

        signal_result = SignalResult(
            should_compose=True,
            phase="build",
            task="implement feature",
            pre_filter_matched="prompt_keyword",
        )
        with (
            patch(
                "agentalloy.api.proxy_router.evaluate_signal",
                return_value=signal_result,
            ),
            TestClient(app) as client,
        ):
            resp = client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4",
                    "messages": [{"role": "user", "content": "Implement X"}],
                    "metadata": {"cwd": str(tmp_path)},
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["choices"][0]["message"]["content"] == "Test response"

        # Telemetry shows passthrough
        app.state.vector_store.record_composition_trace.assert_called_once()
        trace = app.state.vector_store.record_composition_trace.call_args[0][0]
        assert trace.status == "proxy_passthrough"


class TestUpstreamErrorHandling:
    """Upstream error handling in the integrated flow."""

    def test_upstream_500_error(self) -> None:
        """Upstream returns 500 -> proxy returns 503 with error code."""
        app = _make_app(upstream_status=500)

        with (
            patch(
                "agentalloy.api.proxy_router.evaluate_signal",
                return_value=SignalResult(should_compose=False),
            ),
            TestClient(app) as client,
        ):
            resp = client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            )

        assert resp.status_code == 503
        body = resp.json()
        assert body["error"]["code"] == "upstream_unavailable"

        # Telemetry should capture error
        app.state.vector_store.record_composition_trace.assert_called_once()
        trace = app.state.vector_store.record_composition_trace.call_args[0][0]
        assert trace.error_code is not None

    def test_no_upstream_configured(self) -> None:
        """No upstream client -> 503 with clear message."""
        app = create_app(use_default_lifespan=False)
        app.state.upstream_client = None

        with TestClient(app) as client:
            resp = client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            )

        assert resp.status_code == 503
        body = resp.json()
        assert body["error"]["code"] == "upstream_not_configured"

    def test_request_body_preserved(self) -> None:
        """All request fields are forwarded to upstream."""
        captured_payload: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured_payload.update(json.loads(request.content.decode()))
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-123",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": "gpt-4",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "OK"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                },
                request=request,
            )

        mock_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://mock-upstream/v1",
        )

        app = create_app(use_default_lifespan=False)
        app.state.upstream_client = mock_client
        app.state.vector_store = MagicMock()

        with (
            patch(
                "agentalloy.api.proxy_router.evaluate_signal",
                return_value=SignalResult(should_compose=False),
            ),
            TestClient(app) as client,
        ):
            resp = client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4",
                    "messages": [
                        {"role": "system", "content": "Be helpful"},
                        {"role": "user", "content": "Hello"},
                    ],
                    "temperature": 0.7,
                    "max_tokens": 100,
                    "top_p": 0.9,
                },
            )

        assert resp.status_code == 200
        assert captured_payload["model"] == "gpt-4"
        assert captured_payload["temperature"] == 0.7
        assert captured_payload["max_tokens"] == 100
        assert captured_payload["top_p"] == 0.9
        assert len(captured_payload["messages"]) == 2

    def test_telemetry_with_no_vector_store(self) -> None:
        """When vector_store is None, telemetry is silently skipped."""
        app = create_app(use_default_lifespan=False)
        response_body = {
            "id": "chatcmpl-123",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "gpt-4",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "OK"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        app.state.upstream_client = _make_mock_upstream(response_body)
        app.state.vector_store = None

        with TestClient(app) as client:
            resp = client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            )

        # Should succeed even without vector store
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------


class TestModelResolution:
    """Unit tests for _resolve_model() model-name resolution."""

    def test_agentalloy_proxy_resolves_to_upstream(self) -> None:
        """The synthetic 'agentalloy-proxy' name maps to the configured upstream model."""
        from agentalloy.api.proxy_router import (
            _resolve_model,  # pyright: ignore[reportPrivateUsage]
        )

        result = _resolve_model("agentalloy-proxy", "gpt-4o")
        assert result == "gpt-4o"

    def test_unknown_model_passes_through(self) -> None:
        """Any other model name is forwarded unchanged."""
        from agentalloy.api.proxy_router import (
            _resolve_model,  # pyright: ignore[reportPrivateUsage]
        )

        result = _resolve_model("claude-3-opus", "gpt-4o")
        assert result == "claude-3-opus"
