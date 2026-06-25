"""OpenAI-surface marker parity — `/v1/chat/completions`.

The OpenAI chat-completions path now runs the SAME
``evaluate_signal → compose → inject → commit_markers`` cycle as the native
Anthropic passthrough, via the shared :func:`agentalloy.api.proxy_apply.apply_signal`
seam. These tests mirror the cadence-marker guards in
``tests/test_proxy_passthrough_native.py``: the announce marker is committed only
after a confirmed, non-empty injection, and never when compose degrades, when
there is no user message to inject, or for a tool-less (carrier-gated) request.

Hermetic e2e: real FastAPI app (no lifespan) + a mock OpenAI upstream
(httpx.MockTransport) + a stub orchestrator; ``evaluate_signal`` is patched to a
known SignalResult to isolate the inject/commit wiring.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
from fastapi.testclient import TestClient

from agentalloy.api.compose_models import ComposedResult, LatencyBreakdown
from agentalloy.api.proxy_signal import SignalResult
from agentalloy.app import create_app
from agentalloy.orchestration.compose import ComposeOrchestrator

_SIGNAL = "agentalloy.api.proxy_router.evaluate_signal"


def _upstream() -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": "gpt-4",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
            request=request,
        )

    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://mock-upstream/v1"
    )


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


def _make_app(orchestrator: ComposeOrchestrator | None = None) -> Any:
    app = create_app(use_default_lifespan=False)
    app.state.upstream_client = _upstream()
    app.state.embed_client = MagicMock()
    app.state.vector_store = MagicMock()
    if orchestrator is not None:
        from agentalloy.api.compose_router import get_orchestrator

        app.dependency_overrides[get_orchestrator] = lambda: orchestrator
    return app


def _body(cwd: Path, *, tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": "gpt-4",
        "messages": [
            {"role": "system", "content": "SYSTEM-CACHED"},
            {"role": "user", "content": "the real task"},
        ],
        "metadata": {"cwd": str(cwd)},
    }
    if tools is not None:
        payload["tools"] = tools
    return payload


def _no_user_body(cwd: Path) -> dict[str, Any]:
    return {
        "model": "gpt-4",
        "messages": [
            {"role": "system", "content": "SYSTEM-CACHED"},
            {"role": "assistant", "content": "no user turn here"},
        ],
        "metadata": {"cwd": str(cwd)},
    }


def _announced_file(tmp_path: Path) -> str | None:
    f = tmp_path / ".agentalloy" / "announced"
    return f.read_text().strip() if f.exists() else None


# (a) announce marker written after a delivered Tier-1 block.
def test_announce_marker_committed_after_delivery(tmp_path: Path) -> None:
    (tmp_path / ".agentalloy").mkdir()
    app = _make_app(orchestrator=_orchestrator("ORIENTATION-PROSE"))
    signal = SignalResult(
        should_compose=True,
        announce=True,
        phase="build",
        task="the real task",
        workflow_prose="operate like so",
        pending_announce=("build", ["sess-1"]),
    )
    with patch(_SIGNAL, return_value=signal), TestClient(app) as client:
        resp = client.post("/v1/chat/completions", json=_body(tmp_path))
    assert resp.status_code == 200
    # The marker is committed for (phase, session) after a delivered block.
    assert _announced_file(tmp_path) == "build\tsess-1"


# (b) NOT written when compose degrades to empty.
def test_announce_marker_not_committed_when_compose_degrades(tmp_path: Path) -> None:
    (tmp_path / ".agentalloy").mkdir()
    # No workflow prose + the system leg composes to empty → nothing to inject.
    app = _make_app(orchestrator=_orchestrator(""))
    signal = SignalResult(
        should_compose=True,
        announce=True,
        phase="build",
        task="t",
        workflow_prose=None,
        pending_announce=("build", ["sess-1"]),
    )
    with patch(_SIGNAL, return_value=signal), TestClient(app) as client:
        resp = client.post("/v1/chat/completions", json=_body(tmp_path))
    assert resp.status_code == 200
    # Marker NOT burned → re-announces next turn.
    assert _announced_file(tmp_path) is None


# (c) NOT written when there's no user message to inject.
def test_announce_marker_not_committed_when_no_user_message(tmp_path: Path) -> None:
    (tmp_path / ".agentalloy").mkdir()
    app = _make_app(orchestrator=_orchestrator("ORIENTATION-PROSE"))
    signal = SignalResult(
        should_compose=True,
        announce=True,
        phase="build",
        task="t",
        workflow_prose="operate like so",
        pending_announce=("build", ["sess-1"]),
    )
    with patch(_SIGNAL, return_value=signal), TestClient(app) as client:
        resp = client.post("/v1/chat/completions", json=_no_user_body(tmp_path))
    assert resp.status_code == 200
    # Real orientation text composed, but no user message to inject into →
    # inject_into_openai_messages returns None → marker NOT committed.
    assert _announced_file(tmp_path) is None


# (d) carrier gate: a tools=None request does not announce.
def test_carrier_gate_tools_none_does_not_announce(tmp_path: Path) -> None:
    (tmp_path / ".agentalloy").mkdir()
    app = _make_app(orchestrator=_orchestrator("ORIENTATION-PROSE"))

    # The carrier gate lives in evaluate_signal: a tool-less request yields
    # should_compose=False (no announce). Simulate that decision per-request so the
    # router-side commit wiring is exercised exactly as it would be in production.
    def _fake_signal(request: Any, *_a: object, **_k: object) -> SignalResult:
        if request.tools:
            return SignalResult(
                should_compose=True,
                announce=True,
                phase="build",
                task="t",
                workflow_prose="operate like so",
                pending_announce=("build", ["sess-1"]),
            )
        return SignalResult(should_compose=False)

    with patch(_SIGNAL, side_effect=_fake_signal), TestClient(app) as client:
        # tools=None → carrier-gated → no announce, no marker.
        resp = client.post("/v1/chat/completions", json=_body(tmp_path, tools=None))
        assert resp.status_code == 200
        assert _announced_file(tmp_path) is None

        # A real agent turn carrying tools DOES announce (control).
        resp2 = client.post(
            "/v1/chat/completions",
            json=_body(tmp_path, tools=[{"type": "function", "function": {"name": "x"}}]),
        )
        assert resp2.status_code == 200
        assert _announced_file(tmp_path) == "build\tsess-1"
