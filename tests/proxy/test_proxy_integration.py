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

import contextlib
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
    captured: dict[str, Any] | None = None,
) -> httpx.AsyncClient:
    """Create an httpx.AsyncClient with MockTransport for the upstream LLM.

    When *captured* is supplied, the forwarded JSON payload is recorded into it
    under ``"payload"`` so tests can assert what was actually sent upstream.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None and request.content:
            with contextlib.suppress(ValueError):
                captured["payload"] = json.loads(request.content.decode())
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
    mock_telemetry_store: Any = None,
    raise_upstream: Exception | None = None,
    upstream_status: int = 200,
    stream_chunks: list[str] | None = None,
    captured: dict[str, Any] | None = None,
    mock_phase_telemetry: Any = None,
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
        captured=captured,
    )

    # Embed client (mock)
    mock_embed = MagicMock()
    app.state.embed_client = mock_embed

    # Vector store (mock) -- FragmentStore role (search retrieval), NOT the
    # telemetry sink. In v5 the proxy reads search from app.state.vector_store.
    if mock_vector_store is None:
        mock_vector_store = MagicMock()
    app.state.vector_store = mock_vector_store

    # Telemetry store (mock) -- the proxy trace sink. In v5 get_vector_store()
    # resolves app.state.telemetry_store and writes record_composition_trace there.
    if mock_telemetry_store is None:
        mock_telemetry_store = MagicMock()
    app.state.telemetry_store = mock_telemetry_store

    # Phase-event writer (task 04) -- app-state-scoped, mirroring app.py's
    # lifespan wiring (create_app(use_default_lifespan=False) skips lifespan,
    # so tests must set this explicitly, same as the other app.state fields
    # above). Defaults to a MagicMock so tests can assert phase/llm_* writes
    # without a real DuckDB.
    if mock_phase_telemetry is None:
        mock_phase_telemetry = MagicMock()
    app.state.phase_telemetry = mock_phase_telemetry

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
        app.state.telemetry_store.record_composition_trace.assert_called_once()
        trace = app.state.telemetry_store.record_composition_trace.call_args[0][0]
        assert trace.status == "proxy_passthrough"

    def test_signal_match_compose_and_inject(self, tmp_path: Path) -> None:
        """Signal match -> compose -> inject into the last user message ONLY.

        Parity with the Anthropic passthrough: the retrieval-derived block is leg 1
        and lands in the last user message (phase-stamped). It is NOT copied into
        the system message -- that is leg 3's target, and leg 3 carries different
        content (the phase prose) on a different cadence. This fixture leaves
        `workflow_system_prose` unset, so the system message must come through
        byte-identical.
        """
        compose_output = "# Skill: Test\nAlways be helpful."
        orchestrator = _make_mock_orchestrator(compose_output=compose_output)
        captured: dict[str, Any] = {}
        app = _make_app(mock_orchestrator=orchestrator, captured=captured)

        # announce=True + workflow_prose so the Tier 1 orientation block composes.
        signal_result = SignalResult(
            should_compose=True,
            announce=True,
            phase="build",
            task="implement feature",
            workflow_prose="operate like so",
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

        # The composed block landed in the LAST user message, phase-stamped.
        sent = captured["payload"]
        last_user = sent["messages"][-1]
        assert last_user["role"] == "user"
        assert "phase=build" in last_user["content"]
        assert compose_output in last_user["content"]
        # The system message is untouched: leg 1 does not write it, and leg 3 is
        # silent because this fixture sets no `workflow_system_prose`.
        assert sent["messages"][0]["role"] == "system"
        assert sent["messages"][0]["content"] == "You are an assistant."

        # Telemetry should show composed status
        app.state.telemetry_store.record_composition_trace.assert_called_once()
        trace = app.state.telemetry_store.record_composition_trace.call_args[0][0]
        assert trace.status == "proxy_composed"

    def test_no_op_when_user_and_system_already_marked(self, tmp_path: Path) -> None:
        """Same as above with the system message ALSO pre-marked.

        Leg 1 no-ops on the user message; leg 3 is silent (no
        `workflow_system_prose`) and would no-op on the pre-marked system message
        anyway. Request forwarded unchanged, telemetry reports passthrough."""
        compose_output = "# Skill: Test\nAlways be helpful."
        orchestrator = _make_mock_orchestrator(compose_output=compose_output)
        captured: dict[str, Any] = {}
        app = _make_app(mock_orchestrator=orchestrator, captured=captured)

        marker_block = (
            "<!-- BEGIN AGENTALLOY-CONTEXT phase=build -->\nx\n<!-- END AGENTALLOY-CONTEXT -->"
        )
        messages = [
            {"role": "system", "content": f"You are an assistant.\n\n{marker_block}"},
            {"role": "user", "content": f"Implement feature X\n\n{marker_block}"},
        ]
        signal_result = SignalResult(
            should_compose=True,
            announce=True,
            phase="build",
            task="implement feature",
            workflow_prose="operate like so",
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
                    "messages": messages,
                    "metadata": {"cwd": str(tmp_path)},
                },
            )

        assert resp.status_code == 200
        sent = captured["payload"]
        assert sent["messages"] == messages

        app.state.telemetry_store.record_composition_trace.assert_called_once()
        trace = app.state.telemetry_store.record_composition_trace.call_args[0][0]
        assert trace.status == "proxy_passthrough"

    def test_user_leg_no_op_is_not_a_delivery(self, tmp_path: Path) -> None:
        """Leg 1's user message is already marked -> nothing delivered -> passthrough.

        This used to record a composition, because leg 1 had a system-message
        fallback: a system-only write counted as delivery. Leg 1 is now
        user-message-only (the system message belongs to leg 3, which carries the
        phase prose, not the composed block), so a no-op on the user leg is a no-op
        for the whole composition -- and the announce cadence must not be burned.
        """
        compose_output = "# Skill: Test\nAlways be helpful."
        orchestrator = _make_mock_orchestrator(compose_output=compose_output)
        captured: dict[str, Any] = {}
        app = _make_app(mock_orchestrator=orchestrator, captured=captured)

        marker_block = (
            "<!-- BEGIN AGENTALLOY-CONTEXT phase=build -->\nx\n<!-- END AGENTALLOY-CONTEXT -->"
        )
        user_content = f"Implement feature X\n\n{marker_block}"
        messages = [
            {"role": "system", "content": "You are an assistant."},
            {"role": "user", "content": user_content},
        ]
        signal_result = SignalResult(
            should_compose=True,
            announce=True,
            phase="build",
            task="implement feature",
            workflow_prose="operate like so",
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
                    "messages": messages,
                    "metadata": {"cwd": str(tmp_path)},
                },
            )

        assert resp.status_code == 200
        sent = captured["payload"]
        # Nothing moved: the user message still carries only its pre-existing
        # marker block, and the system message never sees the composed output.
        assert sent["messages"][-1]["content"] == user_content
        assert sent["messages"][0]["content"] == "You are an assistant."
        assert compose_output not in sent["messages"][0]["content"]

        app.state.telemetry_store.record_composition_trace.assert_called_once()
        trace = app.state.telemetry_store.record_composition_trace.call_args[0][0]
        assert trace.status == "proxy_passthrough"

    def test_compose_failure_soft_fail(self, tmp_path: Path) -> None:
        """Signal match but composition fails -> soft-fail passthrough."""
        orchestrator = _make_mock_orchestrator(raise_exc=RuntimeError("compose error"))
        app = _make_app(mock_orchestrator=orchestrator)

        # announce=True drives the Tier 1 compose call, which raises -> _compose_block
        # swallows it (no workflow prose either) -> empty block -> passthrough.
        signal_result = SignalResult(
            should_compose=True,
            announce=True,
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
        app.state.telemetry_store.record_composition_trace.assert_called_once()
        trace = app.state.telemetry_store.record_composition_trace.call_args[0][0]
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
        app.state.telemetry_store.record_composition_trace.assert_called_once()
        trace = app.state.telemetry_store.record_composition_trace.call_args[0][0]
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
        app.state.telemetry_store.record_composition_trace.assert_called_once()

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
        app.state.telemetry_store.record_composition_trace.assert_called_once()
        trace = app.state.telemetry_store.record_composition_trace.call_args[0][0]
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
        app.state.telemetry_store.record_composition_trace.assert_called_once()
        trace = app.state.telemetry_store.record_composition_trace.call_args[0][0]
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
        """When the telemetry sink is None, telemetry is silently skipped."""
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
        # The proxy trace sink is app.state.telemetry_store; None -> skip silently.
        app.state.telemetry_store = None
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


class TestSseUsageScanner:
    """Unit tests for the streaming-usage scanner: `resp.aiter_text()` yields
    byte-boundary chunks, not line-aligned ones, so a `data: {...}` line
    carrying the terminal usage block can be split across two `feed()` calls."""

    def _scanner(self) -> Any:
        from agentalloy.api.proxy_router import (
            _SseUsageScanner,  # pyright: ignore[reportPrivateUsage]
        )

        return _SseUsageScanner()

    def test_usage_within_a_single_chunk(self) -> None:
        scanner = self._scanner()
        scanner.feed('data: {"usage":{"completion_tokens":7}}\n\n')
        assert scanner.latest == 7

    def test_usage_line_split_across_two_chunks(self) -> None:
        """The exact gap the per-chunk version missed: the data: line's bytes
        arrive in two aiter_text() reads."""
        scanner = self._scanner()
        line = 'data: {"usage":{"completion_tokens":99}}\n\n'
        midpoint = len(line) // 2
        scanner.feed(line[:midpoint])
        assert scanner.latest is None  # not yet a complete line
        scanner.feed(line[midpoint:])
        assert scanner.latest == 99

    def test_later_usage_overwrites_earlier(self) -> None:
        scanner = self._scanner()
        scanner.feed('data: {"usage":{"completion_tokens":1}}\n\n')
        scanner.feed('data: {"usage":{"completion_tokens":2}}\n\n')
        assert scanner.latest == 2

    def test_done_marker_and_malformed_json_are_ignored(self) -> None:
        scanner = self._scanner()
        scanner.feed('data: {"choices":[{"delta":{"content":"hi"}}]}\n\n')
        scanner.feed("data: [DONE]\n\n")
        assert scanner.latest is None


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


# ---------------------------------------------------------------------------
# Phase telemetry wiring (Task 04) + llm_* forwarding telemetry (Task 03)
# ---------------------------------------------------------------------------


class TestPhaseTelemetryWiring:
    """T16 — app.state.phase_telemetry is wired and reused; llm_sent/llm_received
    fire around the upstream forward on the non-streaming path."""

    def test_phase_telemetry_writer_is_reachable_from_app_state(self, tmp_path: Path) -> None:
        app = _make_app()
        assert app.state.phase_telemetry is not None

    def test_llm_sent_and_received_emitted_on_success(self, tmp_path: Path) -> None:
        mock_phase_telemetry = MagicMock()
        app = _make_app(mock_phase_telemetry=mock_phase_telemetry)

        with (
            patch(
                "agentalloy.api.proxy_router.evaluate_signal",
                return_value=SignalResult(
                    should_compose=False, trace_id="trace-xyz", phase="build"
                ),
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
        mock_phase_telemetry.llm_sent.assert_called_once()
        sent_args, sent_kwargs = mock_phase_telemetry.llm_sent.call_args
        assert sent_args[0] == "trace-xyz"
        assert sent_kwargs["direction"] == "forward"

        mock_phase_telemetry.llm_received.assert_called_once()
        recv_args, recv_kwargs = mock_phase_telemetry.llm_received.call_args
        assert recv_args[0] == "trace-xyz"
        assert recv_kwargs["success"] is True
        assert recv_kwargs["tokens_out"] == 5  # from the mock upstream's usage block
        mock_phase_telemetry.llm_error.assert_not_called()

    def test_llm_error_emitted_on_connect_error(self, tmp_path: Path) -> None:
        mock_phase_telemetry = MagicMock()
        app = _make_app(
            raise_upstream=httpx.ConnectError("boom"),
            mock_phase_telemetry=mock_phase_telemetry,
        )

        with (
            patch(
                "agentalloy.api.proxy_router.evaluate_signal",
                return_value=SignalResult(
                    should_compose=False, trace_id="trace-err", phase="build"
                ),
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

        assert resp.status_code == 503
        mock_phase_telemetry.llm_error.assert_called_once()
        err_args, err_kwargs = mock_phase_telemetry.llm_error.call_args
        assert err_args[0] == "trace-err"
        assert err_kwargs["success"] is False
        mock_phase_telemetry.llm_received.assert_not_called()

    def test_llm_received_emitted_on_streaming_completion(self, tmp_path: Path) -> None:
        """Streaming path: llm_received fires from inside the SSE generator once
        the stream is drained to completion, with tokens_out from the terminal
        usage chunk (OpenAI stream_options.include_usage shape)."""
        mock_phase_telemetry = MagicMock()
        stream_chunks = [
            'data: {"id":"1","choices":[{"delta":{"content":"Hello"}}]}\n\n',
            'data: {"id":"1","choices":[],"usage":{"completion_tokens":42}}\n\n',
            "data: [DONE]\n\n",
        ]
        app = _make_app(stream_chunks=stream_chunks, mock_phase_telemetry=mock_phase_telemetry)

        with (
            patch(
                "agentalloy.api.proxy_router.evaluate_signal",
                return_value=SignalResult(
                    should_compose=False, trace_id="trace-stream", phase="build"
                ),
            ),
            TestClient(app) as client,
        ):
            resp = client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4",
                    "stream": True,
                    "messages": [{"role": "user", "content": "Hello"}],
                    "metadata": {"cwd": str(tmp_path)},
                },
            )
            # Force the StreamingResponse body to be fully consumed -- the
            # terminal llm_received fires from the generator's tail, which only
            # runs once Starlette has driven it to exhaustion.
            _ = resp.text

        assert resp.status_code == 200
        mock_phase_telemetry.llm_sent.assert_called_once()
        assert mock_phase_telemetry.llm_sent.call_args[0][0] == "trace-stream"

        mock_phase_telemetry.llm_received.assert_called_once()
        recv_args, recv_kwargs = mock_phase_telemetry.llm_received.call_args
        assert recv_args[0] == "trace-stream"
        assert recv_kwargs["tokens_out"] == 42
        assert recv_kwargs["latency_ms"] is not None
        assert recv_kwargs["success"] is True
        mock_phase_telemetry.llm_error.assert_not_called()

    def test_llm_error_emitted_on_streaming_open_failure(self, tmp_path: Path) -> None:
        """A connection failure raised when opening the upstream stream (before
        any chunk is relayed -- MockTransport raises from the request handler,
        which fires at `upstream.stream()` open, not mid-body) emits llm_error,
        not llm_received. The `emitted` guard in the generator makes the
        after-bytes-relayed case correct by the same code path (an exception
        raised inside the `async for` loop skips `_finish_received()` and falls
        through to the same `except` handlers), but MockTransport can't
        cheaply simulate a failure *after* a 200 opens, so that specific path
        isn't separately exercised here."""
        mock_phase_telemetry = MagicMock()
        app = _make_app(
            raise_upstream=httpx.ConnectError("boom"),
            mock_phase_telemetry=mock_phase_telemetry,
        )

        with (
            patch(
                "agentalloy.api.proxy_router.evaluate_signal",
                return_value=SignalResult(
                    should_compose=False, trace_id="trace-stream-err", phase="build"
                ),
            ),
            TestClient(app) as client,
        ):
            resp = client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4",
                    "stream": True,
                    "messages": [{"role": "user", "content": "Hello"}],
                    "metadata": {"cwd": str(tmp_path)},
                },
            )
            _ = resp.text

        assert resp.status_code == 200  # the SSE stream itself opens 200; the error rides in-band
        mock_phase_telemetry.llm_sent.assert_called_once()
        mock_phase_telemetry.llm_error.assert_called_once()
        err_args, err_kwargs = mock_phase_telemetry.llm_error.call_args
        assert err_args[0] == "trace-stream-err"
        assert err_kwargs["success"] is False
        mock_phase_telemetry.llm_received.assert_not_called()

    def test_fresh_writer_fallback_when_app_state_unset(self, tmp_path: Path) -> None:
        """Soft-fail fallback: an app without app.state.phase_telemetry (e.g. an
        older test fixture) still works -- proxy_router falls back to a fresh
        PhaseTelemetryWriter over vector_store instead of raising."""
        app = _make_app()
        del app.state.phase_telemetry

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
