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
from contextlib import closing
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
from fastapi.testclient import TestClient

from agentalloy.api.anthropic_passthrough import AnthropicPassthroughClient
from agentalloy.api.compose_models import ComposedResult, LatencyBreakdown
from agentalloy.api.proxy_context import encode_proj_token
from agentalloy.api.proxy_passthrough_router import (
    _flatten_text_field,
    _normalize_nonleading_system,
    _payload_system_prompt_sha,
    _should_normalize_system,
)
from agentalloy.api.proxy_signal import SignalResult
from agentalloy.app import create_app
from agentalloy.orchestration.compose import ComposeOrchestrator
from agentalloy.storage.telemetry_store import DuckDBTelemetryStore, open_telemetry_store
from tests.harness_e2e.upstream_stub import start_upstream_stub
from tests.support import read_announced_raw

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
            json={"type": "message", "id": "msg_1", "role": "assistant", "content": []},
            request=request,
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _make_app(
    captured: dict[str, Any],
    *,
    orchestrator: ComposeOrchestrator | None = None,
    sse: bytes | None = None,
    status: int = 200,
) -> Any:
    app = create_app(use_default_lifespan=False)
    app.state.anthropic_passthrough_client = AnthropicPassthroughClient(
        upstream_base_url="http://mock-upstream",
        client=_make_upstream(captured, sse=sse, status=status),
    )
    app.state.embed_client = MagicMock()
    # The proxy trace sink: get_vector_store resolves app.state.telemetry_store
    # (a TelemetryStore on telemetry.duck) in v5.
    app.state.telemetry_store = MagicMock()
    if orchestrator is not None:
        from agentalloy.api.compose_router import get_orchestrator

        app.dependency_overrides[get_orchestrator] = lambda: orchestrator
    return app


def _make_app_with_store(
    captured: dict[str, Any],
    store: DuckDBTelemetryStore,
    *,
    orchestrator: ComposeOrchestrator | None = None,
    sse: bytes | None = None,
    status: int = 200,
) -> Any:
    """Like ``_make_app`` but with a real TelemetryStore wired in for telemetry asserts."""
    app = _make_app(captured, orchestrator=orchestrator, sse=sse, status=status)
    app.state.telemetry_store = store
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


def test_inject_into_last_user_message_system_also_injected(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}
    app = _make_app(captured, orchestrator=_orchestrator("INJECTED-PROSE"))
    # announce=True: an entry turn emits the orchestrator orientation block.
    signal = SignalResult(
        should_compose=True,
        announce=True,
        phase="build",
        task="the real task",
        workflow_system_prose="WORKFLOW-PROSE",
    )
    with patch(_SIGNAL, return_value=signal), TestClient(app) as client:
        resp = client.post(f"/proj/{_token(tmp_path)}/v1/messages", json=_anthropic_body())
    assert resp.status_code == 200
    sent = json.loads(captured["body"])
    # The system field carries the phase-pure WORKFLOW prose (its own per-turn leg),
    # with the harness's original system text preserved alongside it.
    assert "SYSTEM-CACHED-BLOCK" in sent["system"]
    assert '<agentalloy-instructions phase="build">' in sent["system"]
    assert "WORKFLOW-PROSE" in sent["system"]
    # ...and NOT the turn-varying composed output, which belongs on the message leg.
    assert "INJECTED-PROSE" not in sent["system"]
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
    signal = SignalResult(
        should_compose=True,
        announce=True,
        phase="build",
        task="t",
        workflow_system_prose="WORKFLOW-PROSE",
    )
    with patch(_SIGNAL, return_value=signal), TestClient(app) as client:
        client.post(f"/proj/{_token(tmp_path)}/v1/messages", json=body)
    sent = json.loads(captured["body"])
    # No second injection into the user message: still exactly one marker there.
    assert sent["messages"][-1]["content"].count("BEGIN AGENTALLOY-CONTEXT") == 1
    # The system field was NOT pre-marked, so the workflow leg still fires
    # independently even though the user-message leg no-opped.
    assert '<agentalloy-instructions phase="build">' in sent["system"]
    assert "WORKFLOW-PROSE" in sent["system"]


def test_both_legs_no_op_when_both_already_marked(tmp_path: Path) -> None:
    """Both the user message and the system field already carry the current-phase
    marker -> both legs no-op -> request forwarded unchanged, cadence marker not
    burned.

    `workflow_system_prose` is populated deliberately: without it the system leg has
    nothing to inject and this test would pass vacuously, proving nothing about the
    marker. With it, the leg is genuinely asked to inject and short-circuits on the
    phase marker already present in `system`.
    """
    (tmp_path / ".agentalloy").mkdir()
    captured: dict[str, Any] = {}
    app = _make_app(captured, orchestrator=_orchestrator("INJECTED-PROSE"))
    body = _anthropic_body()
    marker_block = (
        "<!-- BEGIN AGENTALLOY-CONTEXT phase=build -->\nx\n<!-- END AGENTALLOY-CONTEXT -->"
    )
    xml_marker_block = '<agentalloy-instructions phase="build">\nx\n</agentalloy-instructions>'
    body["messages"][-1]["content"] = f"the real task\n\n{marker_block}"
    body["system"] = f"SYSTEM-CACHED-BLOCK\n\n{xml_marker_block}"
    signal = SignalResult(
        should_compose=True,
        announce=True,
        phase="build",
        task="t",
        workflow_system_prose="operate like so",
        pending_announce=("build", ["sess-1"]),
    )
    with patch(_SIGNAL, return_value=signal), TestClient(app) as client:
        resp = client.post(f"/proj/{_token(tmp_path)}/v1/messages", json=body)
    assert resp.status_code == 200
    assert json.loads(captured["body"]) == body
    # The system leg was asked to inject and declined on the phase marker — the prose
    # is absent precisely because the marker short-circuited it, not because the leg
    # had nothing to say.
    assert "operate like so" not in json.dumps(json.loads(captured["body"]))
    assert _announced_file(tmp_path) is None


def test_system_leg_fires_but_undelivered_message_leg_holds_marker(tmp_path: Path) -> None:
    """User-message leg no-ops (already marked); the workflow system leg still fires.

    The two legs are now independent, so the system leg's success does NOT stand in
    for the message leg: the composed domain block never reached the request, so the
    cadence marker stays unwritten and Tier 1 re-fires next turn. The workflow prose
    itself is not at risk either way — its leg re-sends it every carrier turn.
    """
    (tmp_path / ".agentalloy").mkdir()
    captured: dict[str, Any] = {}
    app = _make_app(captured, orchestrator=_orchestrator("INJECTED-PROSE"))
    body = _anthropic_body()
    body["messages"][-1]["content"] = (
        "the real task\n\n<!-- BEGIN AGENTALLOY-CONTEXT phase=build -->\nx\n<!-- END AGENTALLOY-CONTEXT -->"
    )
    signal = SignalResult(
        should_compose=True,
        announce=True,
        phase="build",
        task="t",
        workflow_system_prose="WORKFLOW-PROSE",
        pending_announce=("build", ["sess-1"]),
    )
    with patch(_SIGNAL, return_value=signal), TestClient(app) as client:
        resp = client.post(f"/proj/{_token(tmp_path)}/v1/messages", json=body)
    assert resp.status_code == 200
    sent = json.loads(captured["body"])
    # System field received the workflow prose; user message unchanged from input.
    assert '<agentalloy-instructions phase="build">' in sent["system"]
    assert "WORKFLOW-PROSE" in sent["system"]
    assert sent["messages"][-1]["content"] == body["messages"][-1]["content"]
    # The composed block was NOT delivered -> marker withheld, re-fires next turn.
    assert _announced_file(tmp_path) is None


def test_tc11_sse_relay_byte_for_byte(tmp_path: Path) -> None:
    """A complete stream (ending in ``message_stop``) is relayed byte-for-byte
    with no corrective chunk appended (1.1 regression)."""
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
    assert resp.headers["content-type"].startswith("text/event-stream")
    # The stream already carries its surface terminal event (message_stop), so
    # the relay is byte-for-byte — the old OpenAI-shaped corrective chunk is
    # never appended.
    assert resp.content == sse
    assert b"finish_reason" not in resp.content
    assert b"[DONE]" not in resp.content


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


# --------------------------------------------------------------------------- #
# Cadence markers are committed only after a confirmed, non-empty injection.
# Regression guard for the "marker-before-inject" bug: a degraded compose used to
# record the phase as oriented while injecting nothing, permanently burning it.
# --------------------------------------------------------------------------- #


def _announced_file(tmp_path: Path) -> str | None:
    return read_announced_raw(tmp_path)


def test_announce_marker_committed_after_delivery(tmp_path: Path) -> None:
    (tmp_path / ".agentalloy").mkdir()
    captured: dict[str, Any] = {}
    app = _make_app(captured, orchestrator=_orchestrator("ORIENTATION-PROSE"))
    # Entry turn with a pending marker and real orientation prose → Tier 1 delivers.
    signal = SignalResult(
        should_compose=True,
        announce=True,
        phase="build",
        task="the real task",
        workflow_prose="operate like so",
        # Same prose on the system-prompt leg, as `evaluate_signal` populates it on
        # every carrier turn. The user-message leg no longer carries workflow prose.
        workflow_system_prose="operate like so",
        pending_announce=("build", ["sess-1"]),
    )
    with patch(_SIGNAL, return_value=signal), TestClient(app) as client:
        resp = client.post(f"/proj/{_token(tmp_path)}/v1/messages", json=_anthropic_body())
    assert resp.status_code == 200
    # The block reached upstream AND the marker is committed for (phase, session).
    assert b"operate like so" in captured["body"]
    assert _announced_file(tmp_path) == "build\tsess-1"


def test_announce_marker_not_committed_when_compose_degrades(tmp_path: Path) -> None:
    (tmp_path / ".agentalloy").mkdir()
    captured: dict[str, Any] = {}
    # No workflow prose + the system leg composes to empty → nothing to inject.
    app = _make_app(captured, orchestrator=_orchestrator(""))
    signal = SignalResult(
        should_compose=True,
        announce=True,
        phase="build",
        task="t",
        workflow_prose=None,
        pending_announce=("build", ["sess-1"]),
    )
    with patch(_SIGNAL, return_value=signal), TestClient(app) as client:
        resp = client.post(f"/proj/{_token(tmp_path)}/v1/messages", json=_anthropic_body())
    assert resp.status_code == 200
    # Original body forwarded unchanged AND the marker is NOT burned → re-announces.
    assert json.loads(captured["body"]) == _anthropic_body()
    assert _announced_file(tmp_path) is None


def _entry_signal() -> SignalResult:
    return SignalResult(
        should_compose=True,
        announce=True,
        phase="build",
        task="the real task",
        workflow_prose="operate like so",
        # Same prose on the system-prompt leg, as `evaluate_signal` populates it on
        # every carrier turn. The user-message leg no longer carries workflow prose.
        workflow_system_prose="operate like so",
        pending_announce=("build", ["sess-1"]),
    )


def test_announce_marker_not_committed_on_upstream_529(tmp_path: Path) -> None:
    """The orientation-drop regression: injected, but upstream overloaded (529).

    The block reaches the forwarded request, but the model never processed it, so
    the cadence marker MUST stay unwritten — the harness retries and we re-announce.
    """
    (tmp_path / ".agentalloy").mkdir()
    captured: dict[str, Any] = {}
    app = _make_app(captured, orchestrator=_orchestrator("ORIENTATION-PROSE"), status=529)
    with patch(_SIGNAL, return_value=_entry_signal()), TestClient(app) as client:
        resp = client.post(f"/proj/{_token(tmp_path)}/v1/messages", json=_anthropic_body())
    assert resp.status_code == 529
    # The block WAS injected into the forwarded request...
    assert b"operate like so" in captured["body"]
    # ...but the non-2xx forward must NOT burn the marker.
    assert _announced_file(tmp_path) is None


def test_announce_marker_committed_then_not_reburned_across_retry(tmp_path: Path) -> None:
    """529 leaves the marker unset; the retry (200) injects again and commits once."""
    (tmp_path / ".agentalloy").mkdir()
    # First attempt: 529 → no commit.
    cap1: dict[str, Any] = {}
    app1 = _make_app(cap1, orchestrator=_orchestrator("ORIENTATION-PROSE"), status=529)
    with patch(_SIGNAL, return_value=_entry_signal()), TestClient(app1) as client:
        client.post(f"/proj/{_token(tmp_path)}/v1/messages", json=_anthropic_body())
    assert _announced_file(tmp_path) is None
    # Retry: 200 → block re-injected (announce still True) and marker committed.
    cap2: dict[str, Any] = {}
    app2 = _make_app(cap2, orchestrator=_orchestrator("ORIENTATION-PROSE"), status=200)
    with patch(_SIGNAL, return_value=_entry_signal()), TestClient(app2) as client:
        resp = client.post(f"/proj/{_token(tmp_path)}/v1/messages", json=_anthropic_body())
    assert resp.status_code == 200
    assert b"operate like so" in cap2["body"]
    assert _announced_file(tmp_path) == "build\tsess-1"


def test_announce_marker_not_committed_on_streaming_529(tmp_path: Path) -> None:
    """Same guard on the streaming surface: status is known at stream open."""
    (tmp_path / ".agentalloy").mkdir()
    captured: dict[str, Any] = {}
    app = _make_app(
        captured, orchestrator=_orchestrator("ORIENTATION-PROSE"), sse=b"data: {}\n\n", status=529
    )
    with patch(_SIGNAL, return_value=_entry_signal()), TestClient(app) as client:
        resp = client.post(
            f"/proj/{_token(tmp_path)}/v1/messages", json=_anthropic_body(stream=True)
        )
    assert resp.status_code == 529
    assert b"operate like so" in captured["body"]
    assert _announced_file(tmp_path) is None


# --------------------------------------------------------------------------- #
# Per-turn phase banner (Anthropic surface).
# The banner injects on EVERY carrier turn into the last user message, AFTER any
# workflow block, leaving the cached system block byte-identical. It fires even on
# a banner-only turn (no announce / no workflow block).
# --------------------------------------------------------------------------- #

_BANNER = "[agentalloy · build] out.md not yet produced · 1/2 sections (missing: B)"


def test_banner_only_turn_injects_into_last_user(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}
    app = _make_app(captured, orchestrator=_orchestrator("SHOULD-NOT-APPEAR"))
    # should_compose=False → no workflow block; banner set → banner-only injection.
    signal = SignalResult(should_compose=False, phase="build", task="t", banner=_BANNER)
    with patch(_SIGNAL, return_value=signal), TestClient(app) as client:
        resp = client.post(f"/proj/{_token(tmp_path)}/v1/messages", json=_anthropic_body())
    assert resp.status_code == 200
    sent = json.loads(captured["body"])
    last_user = sent["messages"][-1]
    assert last_user["role"] == "user"
    assert _BANNER in last_user["content"]
    assert "BEGIN AGENTALLOY-BANNER" in last_user["content"]
    # No workflow composition happened.
    assert "SHOULD-NOT-APPEAR" not in last_user["content"]
    assert "phase=build" not in last_user["content"]
    # System block byte-identical.
    assert sent["system"] == "SYSTEM-CACHED-BLOCK"


def test_banner_appended_after_workflow_block(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}
    app = _make_app(captured, orchestrator=_orchestrator("INJECTED-PROSE"))
    signal = SignalResult(
        should_compose=True,
        announce=True,
        phase="build",
        task="the real task",
        workflow_prose="operate like so",
        # Same prose on the system-prompt leg, as `evaluate_signal` populates it on
        # every carrier turn. The user-message leg no longer carries workflow prose.
        workflow_system_prose="operate like so",
        banner=_BANNER,
    )
    with patch(_SIGNAL, return_value=signal), TestClient(app) as client:
        resp = client.post(f"/proj/{_token(tmp_path)}/v1/messages", json=_anthropic_body())
    assert resp.status_code == 200
    sent = json.loads(captured["body"])
    content = sent["messages"][-1]["content"]
    # Both blocks present; the banner is the freshest (last) text.
    assert "INJECTED-PROSE" in content
    assert "phase=build" in content
    assert _BANNER in content
    assert content.rstrip().endswith("<!-- END AGENTALLOY-BANNER -->")
    assert content.count("BEGIN AGENTALLOY-BANNER") == 1
    # System has phase prose injected (system prompt injection).
    assert "SYSTEM-CACHED-BLOCK" in sent["system"]


def test_announce_marker_not_committed_when_no_user_message_to_inject(tmp_path: Path) -> None:
    # Tier 1 composes real orientation text; the request has NO user message, so
    # inject_into_anthropic_messages returns the payload UNCHANGED (nowhere to
    # inject) for the workflow leg. The wrapper treats this as an all-or-nothing
    # no-op -- even though the system-prompt leg *could* deliver independently,
    # counting that as "delivered" would burn the announced marker and forfeit
    # the workflow block for the rest of this phase/session (should_compose
    # would never fire again). So the request must be forwarded unchanged and
    # the marker must NOT be committed -- the next turn (once a user message
    # exists) re-announces instead.
    (tmp_path / ".agentalloy").mkdir()
    captured: dict[str, Any] = {}
    app = _make_app(captured, orchestrator=_orchestrator("ORIENTATION-PROSE"))
    body: dict[str, Any] = {
        "model": "claude-test",
        "max_tokens": 100,
        "system": "SYSTEM-CACHED-BLOCK",
        "messages": [{"role": "assistant", "content": "no user turn here"}],
        "stream": False,
    }
    signal = SignalResult(
        should_compose=True,
        announce=True,
        phase="build",
        task="t",
        workflow_prose="operate like so",
        # Same prose on the system-prompt leg, as `evaluate_signal` populates it on
        # every carrier turn. The user-message leg no longer carries workflow prose.
        workflow_system_prose="operate like so",
        pending_announce=("build", ["sess-1"]),
    )
    with patch(_SIGNAL, return_value=signal), TestClient(app) as client:
        resp = client.post(f"/proj/{_token(tmp_path)}/v1/messages", json=body)
    assert resp.status_code == 200
    sent = json.loads(captured["body"])
    # Messages untouched — there was nowhere to put the composed block...
    assert sent["messages"] == body["messages"]
    # ...so the marker is NOT burned and Tier 1 re-announces next turn.
    assert _announced_file(tmp_path) is None
    # The workflow leg is independent of all that: it needs no user message, so the
    # prose still reaches `system`. Losing orientation entirely on such a turn was
    # the old all-or-nothing behavior; only the *marker* has to be withheld.
    assert "operate like so" in sent["system"]
    assert "SYSTEM-CACHED-BLOCK" in sent["system"]


# --------------------------------------------------------------------------- #
# Telemetry: the native passthrough surface persists exactly one consolidated
# CompositionTrace per 2xx forward (mirrors the OpenAI surface's _write_flow_telemetry).
# --------------------------------------------------------------------------- #


def _composed_signal(tmp_path: Path) -> SignalResult:
    return SignalResult(
        should_compose=True,
        phase="build",
        announce=True,
        workflow_prose="OPERATE LIKE THIS",
        workflow_skill_id="wf-build",
        repo=str(tmp_path),
        session_key="sess-1",
        session_source="header",
        task="t",
    )


def test_tc_passthrough_writes_single_passthrough_row(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}
    signal = SignalResult(
        should_compose=False,
        phase="build",
        repo=str(tmp_path),
        session_key="sess-1",
        session_source="header",
        task="the real task",
    )
    with closing(open_telemetry_store(tmp_path / "tele.duck")) as store:
        app = _make_app_with_store(captured, store)
        with patch(_SIGNAL, return_value=signal), TestClient(app) as client:
            resp = client.post(f"/proj/{_token(tmp_path)}/v1/messages", json=_anthropic_body())
        assert resp.status_code == 200
        rows = store.query_traces(limit=10)
        assert len(rows) == 1
        row = rows[0]
        assert row.status == "proxy_passthrough"
        assert row.event_type == "proxy_request"
        assert row.session_key == "sess-1"
        assert row.session_source == "header"
        assert row.repo == str(tmp_path)
        assert row.source_skill_ids == []
        assert row.lm_assist_outcome == "disabled"


def test_tc_composed_writes_composed_row_with_skills(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}
    with closing(open_telemetry_store(tmp_path / "tele.duck")) as store:
        app = _make_app_with_store(captured, store, orchestrator=_orchestrator("WF"))
        with patch(_SIGNAL, return_value=_composed_signal(tmp_path)), TestClient(app) as client:
            resp = client.post(f"/proj/{_token(tmp_path)}/v1/messages", json=_anthropic_body())
        assert resp.status_code == 200
        rows = store.query_traces(limit=10)
        assert len(rows) == 1
        row = rows[0]
        assert row.status == "proxy_composed"
        # The workflow header skill is carried through the merged telemetry.
        assert row.workflow_skill_ids == ["wf-build"]


def test_tc_streaming_writes_exactly_one_row(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}
    with closing(open_telemetry_store(tmp_path / "tele.duck")) as store:
        app = _make_app_with_store(
            captured, store, orchestrator=_orchestrator("WF"), sse=b"data: {}\n\n"
        )
        with patch(_SIGNAL, return_value=_composed_signal(tmp_path)), TestClient(app) as client:
            resp = client.post(
                f"/proj/{_token(tmp_path)}/v1/messages", json=_anthropic_body(stream=True)
            )
            assert resp.status_code == 200
            _ = resp.content  # drain the relay generator
        # Written once at stream open (in _forward_streaming's on_status), not per chunk.
        assert len(store.query_traces(limit=10)) == 1


def test_tc_non2xx_writes_no_row(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}
    with closing(open_telemetry_store(tmp_path / "tele.duck")) as store:
        app = _make_app_with_store(captured, store, orchestrator=_orchestrator("WF"), status=529)
        with patch(_SIGNAL, return_value=_composed_signal(tmp_path)), TestClient(app) as client:
            resp = client.post(f"/proj/{_token(tmp_path)}/v1/messages", json=_anthropic_body())
        assert resp.status_code == 529
        # 2xx gate suppresses the write (the model never processed the turn).
        assert store.query_traces(limit=10) == []


def test_tc_compose_exception_still_forwards_no_row(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}
    with closing(open_telemetry_store(tmp_path / "tele.duck")) as store:
        app = _make_app_with_store(captured, store, orchestrator=_orchestrator("WF"))
        with (
            patch(_SIGNAL, side_effect=RuntimeError("signal boom")),
            TestClient(app) as client,
        ):
            resp = client.post(f"/proj/{_token(tmp_path)}/v1/messages", json=_anthropic_body())
        # The compose-path exception leaves on_status = _noop_status: original
        # forwarded, request succeeds, and no telemetry row is written.
        assert resp.status_code == 200
        assert json.loads(captured["body"]) == _anthropic_body()
        assert store.query_traces(limit=10) == []


# --------------------------------------------------------------------------- #
# #547 sub-4: the native passthrough now also writes a phase_events `llm_sent`
# row (workflow_delivered included), mirroring the OpenAI chat-completions
# surface's `_emit_llm_sent`. Previously this surface wrote NO llm_sent row at
# all — instruction-delivery telemetry existed only for the OpenAI path.
# --------------------------------------------------------------------------- #


def _query_llm_sent(store: DuckDBTelemetryStore) -> list[tuple[Any, ...]]:
    # phase_events is created lazily on the first PhaseTelemetryWriter write, so
    # a run that (correctly) writes nothing leaves the table absent entirely.
    exists = store.query(
        "SELECT 1 FROM information_schema.tables WHERE table_name = 'phase_events'"
    )
    if not exists:
        return []
    return store.query(
        "SELECT phase, model, workflow_skill_id, workflow_delivered, "
        "system_prompt_sha, repo FROM phase_events WHERE event_type = 'llm_sent'"
    )


def test_llm_sent_row_written_on_2xx_with_workflow_delivered_true(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}
    signal = SignalResult(
        should_compose=False,
        phase="build",
        workflow_skill_id="wf-build",
        workflow_system_prose="operate like so",
        repo=str(tmp_path),
        session_key="sess-1",
        session_source="header",
        task="t",
    )
    with closing(open_telemetry_store(tmp_path / "tele.duck")) as store:
        app = _make_app_with_store(captured, store)
        with patch(_SIGNAL, return_value=signal), TestClient(app) as client:
            resp = client.post(f"/proj/{_token(tmp_path)}/v1/messages", json=_anthropic_body())
        assert resp.status_code == 200
        rows = _query_llm_sent(store)
        assert len(rows) == 1
        phase, model, workflow_skill_id, workflow_delivered, system_prompt_sha, repo = rows[0]
        assert phase == "build"
        assert model == "claude-test"
        assert workflow_skill_id == "wf-build"
        assert workflow_delivered is True
        assert system_prompt_sha is not None and system_prompt_sha.startswith("sha256:")
        assert repo == str(tmp_path)


def test_llm_sent_row_written_with_workflow_delivered_false_when_no_prose(
    tmp_path: Path,
) -> None:
    """`_composed_signal` sets no `workflow_system_prose` -- the exact analogue of
    a pause turn (#547 sub-1) where instructions are intentionally suppressed."""
    captured: dict[str, Any] = {}
    with closing(open_telemetry_store(tmp_path / "tele.duck")) as store:
        app = _make_app_with_store(captured, store, orchestrator=_orchestrator("WF"))
        with patch(_SIGNAL, return_value=_composed_signal(tmp_path)), TestClient(app) as client:
            resp = client.post(f"/proj/{_token(tmp_path)}/v1/messages", json=_anthropic_body())
        assert resp.status_code == 200
        rows = _query_llm_sent(store)
        assert len(rows) == 1
        assert rows[0][3] is False  # workflow_delivered


def test_llm_sent_row_not_written_on_non_2xx(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}
    signal = SignalResult(
        should_compose=False,
        phase="build",
        workflow_system_prose="operate like so",
        repo=str(tmp_path),
        task="t",
    )
    with closing(open_telemetry_store(tmp_path / "tele.duck")) as store:
        app = _make_app_with_store(captured, store, status=529)
        with patch(_SIGNAL, return_value=signal), TestClient(app) as client:
            resp = client.post(f"/proj/{_token(tmp_path)}/v1/messages", json=_anthropic_body())
        assert resp.status_code == 529
        assert _query_llm_sent(store) == []


def test_llm_sent_system_prompt_sha_none_when_no_system_field(tmp_path: Path) -> None:
    """The Anthropic body in this suite always carries `system`; verify the sha
    helper degrades to None rather than raising when a request has none."""
    captured: dict[str, Any] = {}
    signal = SignalResult(should_compose=False, phase="build", task="t")
    body = {
        "model": "claude-test",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "hi"}],
        "stream": False,
    }
    with closing(open_telemetry_store(tmp_path / "tele.duck")) as store:
        app = _make_app_with_store(captured, store)
        with patch(_SIGNAL, return_value=signal), TestClient(app) as client:
            resp = client.post(f"/proj/{_token(tmp_path)}/v1/messages", json=body)
        assert resp.status_code == 200
        rows = _query_llm_sent(store)
        assert len(rows) == 1
        assert rows[0][4] is None  # system_prompt_sha


# --------------------------------------------------------------------------- #
# #547 sub-4: unit coverage for the sha helpers themselves (both field shapes
# each surface actually uses: Anthropic's str-or-list-of-blocks `system`,
# Responses' plain-string `instructions`, and the missing/empty degrade path).
# --------------------------------------------------------------------------- #


class TestFlattenTextField:
    def test_plain_string(self) -> None:
        assert _flatten_text_field("hello") == "hello"

    def test_empty_string_is_none(self) -> None:
        assert _flatten_text_field("") is None

    def test_list_of_text_blocks_joined(self) -> None:
        blocks = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
        assert _flatten_text_field(blocks) == "ab"

    def test_list_ignores_non_text_blocks(self) -> None:
        blocks = [{"type": "text", "text": "a"}, {"type": "image", "text": "ignored"}]
        assert _flatten_text_field(blocks) == "a"

    def test_empty_list_is_none(self) -> None:
        assert _flatten_text_field([]) is None

    def test_none_is_none(self) -> None:
        assert _flatten_text_field(None) is None

    def test_unexpected_shape_is_none(self) -> None:
        assert _flatten_text_field(42) is None


class TestPayloadSystemPromptSha:
    def test_string_field_hashes(self) -> None:
        sha = _payload_system_prompt_sha({"system": "SYSTEM TEXT"}, "system")
        assert sha is not None and sha.startswith("sha256:")
        assert sha == _payload_system_prompt_sha({"system": "SYSTEM TEXT"}, "system")

    def test_different_text_different_hash(self) -> None:
        a = _payload_system_prompt_sha({"system": "A"}, "system")
        b = _payload_system_prompt_sha({"system": "B"}, "system")
        assert a != b

    def test_list_field_hashes(self) -> None:
        payload = {"system": [{"type": "text", "text": "SYSTEM TEXT"}]}
        assert _payload_system_prompt_sha(payload, "system") == _payload_system_prompt_sha(
            {"system": "SYSTEM TEXT"}, "system"
        )

    def test_missing_field_is_none(self) -> None:
        assert _payload_system_prompt_sha({}, "system") is None

    def test_instructions_field_name(self) -> None:
        sha = _payload_system_prompt_sha({"instructions": "INSTR"}, "instructions")
        assert sha is not None and sha.startswith("sha256:")


# --------------------------------------------------------------------------- #
# #505/#504 — per-repo .agentalloy/upstream must actually be reached, not just
# echoed by `add`. Uses a REAL listening upstream (the harness matrix's stub)
# rather than a MockTransport, so these assert egress at the socket level:
# the request either lands on the adopted upstream or it doesn't.
# --------------------------------------------------------------------------- #


def _write_upstream(root: Path, text: str) -> None:
    (root / ".agentalloy").mkdir(parents=True, exist_ok=True)
    (root / ".agentalloy" / "upstream").write_text(text)


def test_per_repo_upstream_wins_over_lifespan_default(tmp_path: Path) -> None:
    """A .agentalloy/upstream captured by `agentalloy add` must be the upstream
    this surface actually forwards to -- not the process-wide default the
    lifespan-scoped client was built from. This is the Anthropic-native sibling
    of #505 (the Responses surface), called out explicitly in the issue as a
    second affected surface with the same defect shape."""
    stub = start_upstream_stub()
    try:
        _write_upstream(
            tmp_path,
            f"claude-code:\n  url: {stub.base_url}/v1\n  model: claude-local\n",
        )
        wrong_default = AnthropicPassthroughClient(
            upstream_base_url="http://should-not-be-reached.invalid"
        )
        app = create_app(use_default_lifespan=False)
        app.state.anthropic_passthrough_client = wrong_default
        app.state.embed_client = MagicMock()
        app.state.telemetry_store = MagicMock()
        with (
            patch(_SIGNAL, return_value=SignalResult(should_compose=False)),
            TestClient(app) as client,
        ):
            resp = client.post(
                f"/proj/{_token(tmp_path)}/v1/messages",
                json=_anthropic_body(),
                headers={"authorization": "Bearer caller-key"},
            )
        assert resp.status_code == 200
        assert len(stub.captured) == 1
        assert stub.captured[0].path == "/v1/messages"
        assert stub.captured[0].payload["messages"] == _anthropic_body()["messages"]
    finally:
        stub.stop()


def test_no_credential_injected_when_key_env_present(tmp_path: Path) -> None:
    """The passthrough surfaces are auth-transparent: a per-repo key_env must
    change only the destination, never inject a credential. The caller sent no
    authorization header here, and the adopted upstream must not receive one
    (proving key_env plays no role on this surface)."""
    stub = start_upstream_stub()
    try:
        _write_upstream(
            tmp_path,
            f"claude-code:\n  url: {stub.base_url}/v1\n  model: claude-local\n  key_env: SOME_UNSET_UPSTREAM_KEY\n",
        )
        app = create_app(use_default_lifespan=False)
        app.state.anthropic_passthrough_client = None
        app.state.embed_client = MagicMock()
        app.state.telemetry_store = MagicMock()
        with (
            patch(_SIGNAL, return_value=SignalResult(should_compose=False)),
            TestClient(app) as client,
        ):
            resp = client.post(f"/proj/{_token(tmp_path)}/v1/messages", json=_anthropic_body())
        assert resp.status_code == 200
        assert len(stub.captured) == 1
    finally:
        stub.stop()


def test_falls_back_to_default_when_no_per_repo_upstream(tmp_path: Path) -> None:
    """No .agentalloy/upstream in this repo -> the lifespan-scoped default is
    still used (the fix must not break the no-override path)."""
    captured: dict[str, Any] = {}
    app = _make_app(captured)
    with (
        patch(_SIGNAL, return_value=SignalResult(should_compose=False)),
        TestClient(app) as client,
    ):
        resp = client.post(f"/proj/{_token(tmp_path)}/v1/messages", json=_anthropic_body())
    assert resp.status_code == 200
    assert json.loads(captured["body"]) == _anthropic_body()


# --------------------------------------------------------------------------- #
# Non-leading system-message normalization (non-Anthropic upstreams)
# --------------------------------------------------------------------------- #


def _body_with_trailing_system() -> dict[str, Any]:
    """Claude Code's real shape: a `role: "system"` entry inside `messages`."""
    body = _anthropic_body()
    body["messages"] = [
        {"role": "user", "content": "the real task"},
        {
            "role": "system",
            "content": [{"type": "text", "text": "<system-reminder>x</system-reminder>"}],
        },
    ]
    return body


def test_nonleading_system_message_rewritten_to_user(tmp_path: Path) -> None:
    """A system message after index 0 is forwarded as a user message: local
    chat templates reject a system message that is not at the beginning."""
    captured: dict[str, Any] = {}
    app = _make_app(captured)  # upstream host "mock-upstream" -> normalization on
    with (
        patch(_SIGNAL, return_value=SignalResult(should_compose=False)),
        TestClient(app) as client,
    ):
        resp = client.post(
            f"/proj/{_token(tmp_path)}/v1/messages", json=_body_with_trailing_system()
        )
    assert resp.status_code == 200
    sent = json.loads(captured["body"])
    assert [m["role"] for m in sent["messages"]] == ["user", "user"]
    # Content and position untouched — only the role changed.
    assert sent["messages"][1]["content"] == [
        {"type": "text", "text": "<system-reminder>x</system-reminder>"}
    ]
    assert sent["system"] == "SYSTEM-CACHED-BLOCK"


def test_leading_system_message_left_alone(tmp_path: Path) -> None:
    """A system message AT index 0 renders through every template — untouched."""
    captured: dict[str, Any] = {}
    app = _make_app(captured)
    body = _anthropic_body()
    body["messages"] = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]
    with (
        patch(_SIGNAL, return_value=SignalResult(should_compose=False)),
        TestClient(app) as client,
    ):
        resp = client.post(f"/proj/{_token(tmp_path)}/v1/messages", json=body)
    assert resp.status_code == 200
    assert json.loads(captured["body"]) == body


def test_normalize_returns_same_object_when_nothing_to_rewrite() -> None:
    payload = _anthropic_body()
    assert _normalize_nonleading_system(payload) is payload
    assert _normalize_nonleading_system({"messages": "not-a-list"}) == {"messages": "not-a-list"}


def test_normalize_gate_defaults_by_upstream_host(tmp_path: Path) -> None:
    anthropic = AnthropicPassthroughClient(upstream_base_url="https://api.anthropic.com")
    local = AnthropicPassthroughClient(upstream_base_url="http://127.0.0.1:60011")
    assert _should_normalize_system(tmp_path, anthropic) is False
    assert _should_normalize_system(tmp_path, local) is True


def test_normalize_gate_respects_per_repo_override(tmp_path: Path) -> None:
    (tmp_path / ".agentalloy").mkdir()
    upstream = tmp_path / ".agentalloy" / "upstream"
    local = AnthropicPassthroughClient(upstream_base_url="http://127.0.0.1:60011")
    anthropic = AnthropicPassthroughClient(upstream_base_url="https://api.anthropic.com")

    upstream.write_text(
        "claude-code:\n  url: http://127.0.0.1:60011/v1\n  model: m\n  normalize_system: false\n"
    )
    assert _should_normalize_system(tmp_path, local) is False

    upstream.write_text(
        "claude-code:\n  url: https://api.anthropic.com\n  model: m\n  normalize_system: true\n"
    )
    assert _should_normalize_system(tmp_path, anthropic) is True
