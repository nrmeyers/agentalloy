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
from agentalloy.api.proxy_signal import SignalResult
from agentalloy.app import create_app
from agentalloy.orchestration.compose import ComposeOrchestrator
from agentalloy.storage.telemetry_store import DuckDBTelemetryStore, open_telemetry_store

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


# --------------------------------------------------------------------------- #
# Cadence markers are committed only after a confirmed, non-empty injection.
# Regression guard for the "marker-before-inject" bug: a degraded compose used to
# record the phase as oriented while injecting nothing, permanently burning it.
# --------------------------------------------------------------------------- #


def _announced_file(tmp_path: Path) -> str | None:
    f = tmp_path / ".agentalloy" / "announced"
    return f.read_text().strip() if f.exists() else None


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

_BANNER = "[agentalloy · build] MUST produce out.md before advancing · 1/2 sections (missing: B)"


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
    # System untouched.
    assert sent["system"] == "SYSTEM-CACHED-BLOCK"


def test_announce_marker_not_committed_when_no_user_message_to_inject(tmp_path: Path) -> None:
    # Tier 1 composes real orientation text, but the request has NO user message, so
    # inject_into_anthropic_messages returns the payload UNCHANGED (nowhere to inject).
    # The block never reached Claude → the marker must NOT be committed, so the next
    # turn re-announces instead of the session being silently burned.
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
        pending_announce=("build", ["sess-1"]),
    )
    with patch(_SIGNAL, return_value=signal), TestClient(app) as client:
        resp = client.post(f"/proj/{_token(tmp_path)}/v1/messages", json=body)
    assert resp.status_code == 200
    # Body forwarded unchanged (nothing injected) AND the marker is NOT burned.
    assert json.loads(captured["body"]) == body
    assert _announced_file(tmp_path) is None


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
