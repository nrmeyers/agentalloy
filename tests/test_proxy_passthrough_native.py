"""Native Anthropic passthrough route — `/proj/{token}/v1/messages`.

Hermetic e2e: real FastAPI app (no lifespan) + a mock Anthropic upstream
(httpx.MockTransport) that captures exactly what we forward. Drives the real
route; the signal layer is either exercised for real (lifecycle gate) or
patched to a known SignalResult to isolate inject/forward/soft-fail.

Maps to test-plan TC1, TC2, TC5, TC11, TC12, TC13.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
from fastapi.testclient import TestClient

from agentalloy.api.anthropic_passthrough import AnthropicPassthroughClient
from agentalloy.api.compose_models import ComposedResult, LatencyBreakdown
from agentalloy.api.proxy_context import encode_proj_token
from agentalloy.api.proxy_signal import SignalResult
from agentalloy.app import create_app
from agentalloy.orchestration.compose import ComposeOrchestrator

_SIGNAL = "agentalloy.api.proxy_passthrough_router.evaluate_signal"


def _anthropic_body(*, stream: bool = False) -> dict[str, Any]:
    return {
        "model": "claude-test",
        "max_tokens": 100,
        "system": "SYSTEM-CACHED-BLOCK",
        "messages": [
            {"role": "user", "content": "earlier turn"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "the real task"},
        ],
        "stream": stream,
    }


def _make_upstream(captured: dict[str, Any], *, sse: bytes | None = None) -> httpx.AsyncClient:
    async def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        captured["headers"] = dict(request.headers)
        captured["url"] = str(request.url)
        if sse is not None:

            async def _aiter() -> AsyncIterator[bytes]:
                yield sse

            return httpx.Response(
                200,
                content=_aiter(),
                headers={"content-type": "text/event-stream"},
                request=request,
            )
        return httpx.Response(
            200,
            json={"type": "message", "id": "msg_1", "role": "assistant", "content": []},
            request=request,
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _make_app(
    captured: dict[str, Any],
    *,
    orchestrator: ComposeOrchestrator | None = None,
    sse: bytes | None = None,
) -> Any:
    app = create_app(use_default_lifespan=False)
    app.state.anthropic_passthrough_client = AnthropicPassthroughClient(
        upstream_base_url="http://mock-upstream", client=_make_upstream(captured, sse=sse)
    )
    app.state.embed_client = MagicMock()
    app.state.vector_store = MagicMock()
    if orchestrator is not None:
        from agentalloy.api.compose_router import get_orchestrator

        app.dependency_overrides[get_orchestrator] = lambda: orchestrator
    return app


def _orchestrator(output: str) -> ComposeOrchestrator:
    mock = MagicMock(spec=ComposeOrchestrator)

    async def compose(req: Any, **_kwargs: object) -> ComposedResult:
        return ComposedResult(
            task=getattr(req, "task", "t"),
            phase=getattr(req, "phase", "build"),
            output=output,
            domain_fragments=["f1"],
            source_skills=["s1"],
            system_fragments=[],
            system_skills_applied=False,
            assembly_tier=1,
            latency_ms=LatencyBreakdown(retrieval_ms=1, assembly_ms=1, total_ms=2),
        )

    mock.compose = compose
    return mock


def _token(tmp_path: Path) -> str:
    return encode_proj_token(tmp_path)


# --------------------------------------------------------------------------- #
# TC1 / TC2 — passthrough + forwarding (no composition)
# --------------------------------------------------------------------------- #


def test_tc1_no_translation_body_forwarded_verbatim(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}
    app = _make_app(captured)
    body = _anthropic_body()
    with (
        patch(_SIGNAL, return_value=SignalResult(should_compose=False)),
        TestClient(app) as client,
    ):
        resp = client.post(f"/proj/{_token(tmp_path)}/v1/messages", json=body)
    assert resp.status_code == 200
    # Upstream received the Anthropic-shaped body byte-equivalent (no OpenAI translation).
    sent = json.loads(captured["body"])
    assert sent == body
    assert sent["system"] == "SYSTEM-CACHED-BLOCK"
    assert "choices" not in sent  # not translated to OpenAI shape


def test_tc2_auth_and_beta_headers_pass_through(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}
    app = _make_app(captured)
    with (
        patch(_SIGNAL, return_value=SignalResult(should_compose=False)),
        TestClient(app) as client,
    ):
        resp = client.post(
            f"/proj/{_token(tmp_path)}/v1/messages",
            json=_anthropic_body(),
            headers={
                "authorization": "Bearer sk-ant-oat-SECRET",
                "x-api-key": "sk-ant-api-SECRET",
                "anthropic-beta": "oauth-2025-04-20",
                "anthropic-version": "2023-06-01",
                "x-claude-code-session-id": "sess-123",
                "connection": "keep-alive",
            },
        )
    assert resp.status_code == 200
    h = captured["headers"]
    assert h["authorization"] == "Bearer sk-ant-oat-SECRET"
    assert h["x-api-key"] == "sk-ant-api-SECRET"
    assert h["anthropic-beta"] == "oauth-2025-04-20"
    assert h["anthropic-version"] == "2023-06-01"
    assert h["x-claude-code-session-id"] == "sess-123"
    assert h["host"] == "mock-upstream"  # rewritten to upstream
    # (httpx sets its own per-hop Connection header on the new connection; the
    # denylist's hop-by-hop stripping is unit-tested in test_anthropic_passthrough.)


def test_tc1_query_string_preserved(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}
    app = _make_app(captured)
    with (
        patch(_SIGNAL, return_value=SignalResult(should_compose=False)),
        TestClient(app) as client,
    ):
        client.post(f"/proj/{_token(tmp_path)}/v1/messages?beta=true", json=_anthropic_body())
    assert captured["url"].endswith("/v1/messages?beta=true")


# --------------------------------------------------------------------------- #
# Injection (compose fires) + TC11 streaming
# --------------------------------------------------------------------------- #


def test_inject_into_last_user_message_system_untouched(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}
    app = _make_app(captured, orchestrator=_orchestrator("INJECTED-PROSE"))
    # announce=True: an entry turn emits the orchestrator orientation block.
    signal = SignalResult(should_compose=True, announce=True, phase="build", task="the real task")
    with patch(_SIGNAL, return_value=signal), TestClient(app) as client:
        resp = client.post(f"/proj/{_token(tmp_path)}/v1/messages", json=_anthropic_body())
    assert resp.status_code == 200
    sent = json.loads(captured["body"])
    # system block byte-unchanged (prompt-cache safe)
    assert sent["system"] == "SYSTEM-CACHED-BLOCK"
    # injected into the LAST user message, phase-stamped
    last_user = sent["messages"][-1]
    assert last_user["role"] == "user"
    assert "INJECTED-PROSE" in last_user["content"]
    assert "phase=build" in last_user["content"]
    # earlier user message untouched
    assert sent["messages"][0]["content"] == "earlier turn"


def test_idempotent_when_phase_block_already_present(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}
    app = _make_app(captured, orchestrator=_orchestrator("INJECTED-PROSE"))
    body = _anthropic_body()
    # Simulate a prior turn's injected block already in history.
    body["messages"][-1]["content"] = (
        "the real task\n\n<!-- BEGIN AGENTALLOY-CONTEXT phase=build -->\nx\n<!-- END AGENTALLOY-CONTEXT -->"
    )
    # announce=True so compose actually produces a block; the request-level
    # injector is still idempotent for the current phase (a marker for phase=build
    # already in this payload short-circuits a second injection).
    signal = SignalResult(should_compose=True, announce=True, phase="build", task="t")
    with patch(_SIGNAL, return_value=signal), TestClient(app) as client:
        client.post(f"/proj/{_token(tmp_path)}/v1/messages", json=body)
    sent = json.loads(captured["body"])
    # No second injection: still exactly one marker.
    assert sent["messages"][-1]["content"].count("BEGIN AGENTALLOY-CONTEXT") == 1


def test_tc11_sse_relay_byte_for_byte(tmp_path: Path) -> None:
    sse = b"event: message_start\ndata: {}\n\nevent: message_stop\ndata: {}\n\n"
    captured: dict[str, Any] = {}
    app = _make_app(captured, sse=sse)
    with (
        patch(_SIGNAL, return_value=SignalResult(should_compose=False)),
        TestClient(app) as client,
    ):
        resp = client.post(
            f"/proj/{_token(tmp_path)}/v1/messages", json=_anthropic_body(stream=True)
        )
    assert resp.status_code == 200
    assert resp.content == sse
    assert resp.headers["content-type"].startswith("text/event-stream")


# --------------------------------------------------------------------------- #
# TC12 — soft-fail
# --------------------------------------------------------------------------- #


def test_tc12_compose_error_forwards_original(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}
    app = _make_app(captured, orchestrator=_orchestrator("X"))
    body = _anthropic_body()
    with (
        patch(_SIGNAL, side_effect=RuntimeError("signal boom")),
        TestClient(app) as client,
    ):
        resp = client.post(f"/proj/{_token(tmp_path)}/v1/messages", json=body)
    # Original payload forwarded unchanged; request still succeeds.
    assert resp.status_code == 200
    assert json.loads(captured["body"]) == body


# --------------------------------------------------------------------------- #
# TC13 / TC5 — per-repo lifecycle gate via the REAL signal layer
# --------------------------------------------------------------------------- #


def test_tc13_lifecycle_off_skips_compose_per_repo(tmp_path: Path) -> None:
    # Real evaluate_signal: the token resolves THIS repo, whose config says off.
    agentalloy_dir = tmp_path / ".agentalloy"
    agentalloy_dir.mkdir()
    (agentalloy_dir / "phase").write_text('phase: build\nworkflow: "sdd-build"\n')
    (agentalloy_dir / "config").write_text("lifecycle_mode: off\n")

    captured: dict[str, Any] = {}
    app = _make_app(captured, orchestrator=_orchestrator("SHOULD-NOT-APPEAR"))
    body = _anthropic_body()
    # NOT patching evaluate_signal — exercise the real lifecycle gate.
    with TestClient(app) as client:
        resp = client.post(f"/proj/{_token(tmp_path)}/v1/messages", json=body)
    assert resp.status_code == 200
    # lifecycle=off → no composition → original body forwarded (resolved per-repo,
    # from the URL token, not the proxy's cwd).
    assert json.loads(captured["body"]) == body
