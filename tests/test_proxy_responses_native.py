"""Native OpenAI Responses passthrough route — `/proj/{token}/v1/responses`.

Hermetic e2e mirroring test_proxy_passthrough_native.py: real FastAPI app (no
lifespan) + a mock Responses upstream (httpx.MockTransport) capturing exactly
what we forward. Covers injection into the last user input item, verbatim
passthrough, streaming relay, soft-fail, and the injection helper's shapes.
Spec: docs/responses-surface.md.
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
from agentalloy.api.proxy_context import encode_proj_token
from agentalloy.api.proxy_injection import inject_into_responses_input
from agentalloy.api.proxy_signal import SignalResult
from agentalloy.app import create_app

_SIGNAL = "agentalloy.api.proxy_responses_router.evaluate_signal"


def _responses_body(*, stream: bool = False, string_input: bool = False) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": "gpt-test",
        "instructions": "CACHED-SYSTEM-PROMPT",
        "stream": stream,
        "store": False,
    }
    if string_input:
        body["input"] = "the real task"
    else:
        body["input"] = [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "earlier turn"}],
            },
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "ok"}],
            },
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "the real task"}],
            },
        ]
    return body


def _make_upstream(
    captured: dict[str, Any], *, sse: bytes | None = None, status: int = 200
) -> httpx.AsyncClient:
    async def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        captured["headers"] = dict(request.headers)
        captured["url"] = str(request.url)
        if sse is not None:

            async def _aiter() -> AsyncIterator[bytes]:
                yield sse

            return httpx.Response(
                status,
                content=_aiter(),
                headers={"content-type": "text/event-stream"},
                request=request,
            )
        return httpx.Response(
            status,
            json={"id": "resp_1", "object": "response", "status": "completed", "output": []},
            request=request,
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _make_app(captured: dict[str, Any], *, sse: bytes | None = None, status: int = 200) -> Any:
    app = create_app(use_default_lifespan=False)
    app.state.responses_passthrough_client = AnthropicPassthroughClient(
        upstream_base_url="http://mock-upstream",
        client=_make_upstream(captured, sse=sse, status=status),
    )
    app.state.embed_client = MagicMock()
    app.state.telemetry_store = MagicMock()
    return app


def _no_compose_signal() -> SignalResult:
    return SignalResult(should_compose=False, phase=None, task=None)


def _token(tmp_path: Path) -> str:
    return encode_proj_token(tmp_path)


# --------------------------------------------------------------------------- #
# Route: forward + inject
# --------------------------------------------------------------------------- #


def test_forwards_verbatim_when_nothing_composes(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}
    app = _make_app(captured)
    with (
        patch(_SIGNAL, return_value=_no_compose_signal()),
        TestClient(app) as client,
    ):
        resp = client.post(
            f"/proj/{_token(tmp_path)}/v1/responses",
            json=_responses_body(),
            headers={"authorization": "Bearer caller-key"},
        )
    assert resp.status_code == 200
    forwarded = json.loads(captured["body"])
    assert forwarded == _responses_body()
    assert captured["url"] == "http://mock-upstream/v1/responses"
    # Auth-transparent: the caller's credential is relayed unchanged.
    assert captured["headers"]["authorization"] == "Bearer caller-key"


def test_streaming_relays_sse_bytes(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}
    sse = b'event: response.completed\ndata: {"type": "response.completed"}\n\n'
    app = _make_app(captured, sse=sse)
    with (
        patch(_SIGNAL, return_value=_no_compose_signal()),
        TestClient(app) as client,
    ):
        resp = client.post(
            f"/proj/{_token(tmp_path)}/v1/responses",
            json=_responses_body(stream=True),
        )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.content == sse


def test_banner_injects_into_last_user_item(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}
    app = _make_app(captured)
    signal = SignalResult(
        should_compose=False, phase="build", task="the real task", banner="PHASE: build"
    )
    with (
        patch(_SIGNAL, return_value=signal),
        TestClient(app) as client,
    ):
        resp = client.post(f"/proj/{_token(tmp_path)}/v1/responses", json=_responses_body())
    assert resp.status_code == 200
    forwarded = json.loads(captured["body"])
    # instructions stay byte-identical (prompt caching).
    assert forwarded["instructions"] == "CACHED-SYSTEM-PROMPT"
    last_user = forwarded["input"][2]
    texts = [b["text"] for b in last_user["content"] if b["type"] == "input_text"]
    assert any("AGENTALLOY-BANNER" in t and "PHASE: build" in t for t in texts)
    # Earlier turns untouched.
    assert forwarded["input"][0] == _responses_body()["input"][0]


def test_bad_token_soft_fails_to_verbatim_forward(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}
    app = _make_app(captured)
    with TestClient(app) as client:
        resp = client.post("/proj/%21%21not-a-token/v1/responses", json=_responses_body())
    assert resp.status_code == 200
    assert json.loads(captured["body"]) == _responses_body()


def test_503_when_client_missing(tmp_path: Path) -> None:
    app = create_app(use_default_lifespan=False)
    app.state.responses_passthrough_client = None
    with TestClient(app) as client:
        resp = client.post(f"/proj/{_token(tmp_path)}/v1/responses", json=_responses_body())
    assert resp.status_code == 503


# --------------------------------------------------------------------------- #
# inject_into_responses_input — shapes and idempotence
# --------------------------------------------------------------------------- #


def test_inject_workflow_into_item_list() -> None:
    payload = _responses_body()
    out = inject_into_responses_input(payload, "BLOCK", phase="build")
    assert out is not payload
    texts = [b["text"] for b in out["input"][2]["content"]]
    assert any("AGENTALLOY-CONTEXT phase=build" in t and "BLOCK" in t for t in texts)


def test_inject_workflow_idempotent_for_same_phase() -> None:
    payload = _responses_body()
    once = inject_into_responses_input(payload, "BLOCK", phase="build")
    twice = inject_into_responses_input(once, "BLOCK", phase="build")
    assert twice is once


def test_stale_phase_block_replaced() -> None:
    payload = _responses_body()
    old = inject_into_responses_input(payload, "OLD", phase="spec")
    new = inject_into_responses_input(old, "NEW", phase="build")
    texts = "\n".join(b["text"] for b in new["input"][2]["content"])
    assert "phase=build" in texts and "NEW" in texts
    assert "OLD" not in texts


def test_inject_into_string_input() -> None:
    payload = _responses_body(string_input=True)
    out = inject_into_responses_input(payload, "BLOCK", phase="build")
    assert out is not payload
    assert out["input"].startswith("the real task")
    assert "BLOCK" in out["input"]


def test_banner_strip_and_replace() -> None:
    payload = _responses_body()
    first = inject_into_responses_input(payload, "turn 1/9", phase="build", kind="banner")
    second = inject_into_responses_input(first, "turn 2/9", phase="build", kind="banner")
    texts = "\n".join(b["text"] for b in second["input"][2]["content"])
    assert "turn 2/9" in texts
    assert "turn 1/9" not in texts


def test_no_user_item_is_a_noop() -> None:
    payload = {"model": "m", "input": [{"type": "message", "role": "assistant", "content": []}]}
    out = inject_into_responses_input(payload, "BLOCK", phase="build")
    assert out is payload
