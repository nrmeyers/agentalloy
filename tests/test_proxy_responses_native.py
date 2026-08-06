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
from tests.harness_e2e.upstream_stub import start_upstream_stub

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
    # Leg 2 writes only `input`. This fixture sets no `workflow_system_prose`, so
    # leg 3 is silent and `instructions` comes through byte-identical — pinning the
    # two legs' independence.
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


# --------------------------------------------------------------------------- #
# Leg 3: SDD workflow prose on the top-level `instructions` field.
#
# Fires on EVERY carrier turn — outside the `should_compose` guard, outside
# `apply_signal` — and commits no cadence marker. A "delivered once" record here
# would recreate the bug #499 fixed: codex rebuilds each request from its own local
# history and never observes proxy mutations, so the prose would vanish from turn 2
# on. That is INVISIBLE on a single turn; the two-turn test below is the one that
# catches it.
#
# NOTE for anyone adding coverage here: `evaluate_signal` is patched wholesale and
# `SignalResult` is hand-built, so a fixture that does not explicitly set
# `workflow_system_prose` leaves leg 3 inert and proves nothing.
# --------------------------------------------------------------------------- #

_PROSE = "# SDD — Build\nWork the design's task slices as written."


def _prose_signal(**over: Any) -> SignalResult:
    base: dict[str, Any] = {
        "should_compose": False,
        "phase": "build",
        "task": "the real task",
        "workflow_system_prose": _PROSE,
    }
    base.update(over)
    return SignalResult(**base)


def _post(app: Any, tmp_path: Path, signal: SignalResult, body: dict[str, Any]) -> Any:
    with patch(_SIGNAL, return_value=signal), TestClient(app) as client:
        return client.post(f"/proj/{_token(tmp_path)}/v1/responses", json=body)


def test_prose_lands_on_instructions(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}
    app = _make_app(captured)
    resp = _post(app, tmp_path, _prose_signal(), _responses_body())
    assert resp.status_code == 200

    forwarded = json.loads(captured["body"])
    instructions = forwarded["instructions"]
    assert instructions.startswith("CACHED-SYSTEM-PROMPT")
    assert _PROSE in instructions
    assert instructions.count('<agentalloy-instructions phase="build">') == 1
    assert instructions.count("</agentalloy-instructions>") == 1
    # `input` is leg 1/2 territory — untouched by leg 3.
    assert forwarded["input"] == _responses_body()["input"]


def test_prose_fires_on_a_quiet_turn(tmp_path: Path) -> None:
    """should_compose=False and no banner: leg 3 still delivers."""
    captured: dict[str, Any] = {}
    app = _make_app(captured)
    resp = _post(app, tmp_path, _prose_signal(banner=None), _responses_body())
    assert resp.status_code == 200
    forwarded = json.loads(captured["body"])
    assert _PROSE in forwarded["instructions"]
    assert forwarded["input"] == _responses_body()["input"]


def test_prose_repeats_byte_identically_across_turns(tmp_path: Path) -> None:
    """The deliver-once regression guard; turn 1 alone proves nothing.

    codex rebuilds each request from its own history, so turn 2 sends the SAME body
    it sent on turn 1 — proxy mutations are never echoed back. If leg 3 ever grows a
    cadence marker, turn 2 goes silent here and nowhere else.
    """
    captured: dict[str, Any] = {}
    app = _make_app(captured)
    seen: list[str] = []
    signal = _prose_signal(banner="PHASE: build")
    with patch(_SIGNAL, return_value=signal), TestClient(app) as client:
        for _ in range(2):
            resp = client.post(f"/proj/{_token(tmp_path)}/v1/responses", json=_responses_body())
            assert resp.status_code == 200
            seen.append(json.loads(captured["body"])["instructions"])

    assert _PROSE in seen[0]
    assert seen[1] == seen[0]  # byte-identical within a phase
    assert seen[1].count('<agentalloy-instructions phase="build">') == 1


def test_banner_and_prose_land_on_different_fields(tmp_path: Path) -> None:
    """Legs 2 and 3 are independent: banner → last input item, prose → instructions."""
    captured: dict[str, Any] = {}
    app = _make_app(captured)
    resp = _post(app, tmp_path, _prose_signal(banner="PHASE: build"), _responses_body())
    assert resp.status_code == 200

    forwarded = json.loads(captured["body"])
    instructions = forwarded["instructions"]
    texts = "\n".join(
        b["text"] for b in forwarded["input"][2]["content"] if b["type"] == "input_text"
    )
    assert "AGENTALLOY-BANNER" in texts and "PHASE: build" in texts
    assert _PROSE not in texts
    assert _PROSE in instructions
    assert "AGENTALLOY-BANNER" not in instructions
    # Earlier input items untouched.
    assert forwarded["input"][:2] == _responses_body()["input"][:2]


def test_prose_replaced_on_phase_transition(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}
    app = _make_app(captured)

    _post(app, tmp_path, _prose_signal(), _responses_body())
    build_instructions = json.loads(captured["body"])["instructions"]

    # A harness that persists what it was handed replays the mutated instructions.
    replayed = _responses_body()
    replayed["instructions"] = build_instructions
    qa_prose = "# SDD — QA\nProve the acceptance criteria."
    resp = _post(
        app,
        tmp_path,
        _prose_signal(phase="qa", workflow_system_prose=qa_prose),
        replayed,
    )
    assert resp.status_code == 200

    instructions = json.loads(captured["body"])["instructions"]
    assert 'phase="build"' not in instructions
    assert _PROSE not in instructions
    assert qa_prose in instructions
    assert instructions.count('<agentalloy-instructions phase="qa">') == 1
    assert instructions.startswith("CACHED-SYSTEM-PROMPT")


def test_prose_creates_instructions_when_absent(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}
    app = _make_app(captured)
    body = _responses_body()
    del body["instructions"]
    resp = _post(app, tmp_path, _prose_signal(), body)
    assert resp.status_code == 200
    forwarded = json.loads(captured["body"])
    assert _PROSE in forwarded["instructions"]


def test_prose_alone_forces_the_reserialized_body(tmp_path: Path) -> None:
    """Prose is the ONLY leg firing: the payload must still be recognised as
    injected, so the re-serialized body is forwarded rather than the original bytes."""
    captured: dict[str, Any] = {}
    app = _make_app(captured)
    resp = _post(app, tmp_path, _prose_signal(banner=None), _responses_body())
    assert resp.status_code == 200
    assert json.loads(captured["body"]) != _responses_body()


def test_no_prose_leaves_instructions_byte_identical(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}
    app = _make_app(captured)
    resp = _post(app, tmp_path, _prose_signal(workflow_system_prose=None), _responses_body())
    assert resp.status_code == 200
    assert json.loads(captured["body"])["instructions"] == "CACHED-SYSTEM-PROMPT"


def test_no_anthropic_cache_keys_on_the_responses_wire(tmp_path: Path) -> None:
    """D4: `cache_control`/ttl/breakpoints are Anthropic-only. The Responses API caches
    implicitly with no knob, so none of that may leak into the forwarded body."""
    captured: dict[str, Any] = {}
    app = _make_app(captured)
    resp = _post(app, tmp_path, _prose_signal(banner="PHASE: build"), _responses_body())
    assert resp.status_code == 200
    body = captured["body"].decode()
    assert "cache_control" not in body
    assert '"ttl"' not in body
    assert "ephemeral" not in body


# --------------------------------------------------------------------------- #
# #505 — per-repo .agentalloy/upstream must actually be reached, not just
# echoed by `add codex --upstream-url`. Uses a REAL listening upstream (the
# harness matrix's stub) rather than a MockTransport, asserting egress at the
# socket level: this is the reproduction from the issue (codex talking to
# api.openai.com despite `add` reporting a local upstream), now against a
# deterministic stub instead of the real OpenAI host.
# --------------------------------------------------------------------------- #


def _write_upstream(root: Path, text: str) -> None:
    (root / ".agentalloy").mkdir(parents=True, exist_ok=True)
    (root / ".agentalloy" / "upstream").write_text(text)


def test_per_repo_upstream_wins_over_lifespan_default(tmp_path: Path) -> None:
    stub = start_upstream_stub()
    try:
        _write_upstream(tmp_path, f"url: {stub.base_url}/v1\nmodel: local-model\n")
        wrong_default = AnthropicPassthroughClient(
            upstream_base_url="http://should-not-be-reached.invalid"
        )
        app = create_app(use_default_lifespan=False)
        app.state.responses_passthrough_client = wrong_default
        app.state.embed_client = MagicMock()
        app.state.telemetry_store = MagicMock()
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
        assert len(stub.captured) == 1
        assert stub.captured[0].path == "/v1/responses"
        assert stub.captured[0].payload["model"] == "gpt-test"
    finally:
        stub.stop()


def test_no_credential_injected_when_key_env_present(tmp_path: Path) -> None:
    """Auth-transparent: key_env must never inject a credential on this surface."""
    stub = start_upstream_stub()
    try:
        _write_upstream(
            tmp_path,
            f"url: {stub.base_url}/v1\nmodel: local-model\nkey_env: SOME_UNSET_UPSTREAM_KEY\n",
        )
        app = create_app(use_default_lifespan=False)
        app.state.responses_passthrough_client = None
        app.state.embed_client = MagicMock()
        app.state.telemetry_store = MagicMock()
        with (
            patch(_SIGNAL, return_value=_no_compose_signal()),
            TestClient(app) as client,
        ):
            resp = client.post(f"/proj/{_token(tmp_path)}/v1/responses", json=_responses_body())
        assert resp.status_code == 200
        assert len(stub.captured) == 1
    finally:
        stub.stop()


def test_falls_back_to_default_when_no_per_repo_upstream(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}
    app = _make_app(captured)
    with (
        patch(_SIGNAL, return_value=_no_compose_signal()),
        TestClient(app) as client,
    ):
        resp = client.post(f"/proj/{_token(tmp_path)}/v1/responses", json=_responses_body())
    assert resp.status_code == 200
    assert json.loads(captured["body"]) == _responses_body()
