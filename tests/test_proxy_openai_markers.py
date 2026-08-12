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

import json
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
from tests.support import read_announced_raw

_SIGNAL = "agentalloy.api.proxy_router.evaluate_signal"


def _upstream(captured: dict[str, Any] | None = None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured["body"] = request.content
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


def _make_app(
    orchestrator: ComposeOrchestrator | None = None,
    captured: dict[str, Any] | None = None,
) -> Any:
    app = create_app(use_default_lifespan=False)
    app.state.upstream_client = _upstream(captured)
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
    return read_announced_raw(tmp_path)


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


# --------------------------------------------------------------------------- #
# Per-turn phase banner (OpenAI surface) — leg 2.
# The banner injects on EVERY carrier turn into the last user message of the
# upstream payload, AFTER any workflow block. It fires even on a banner-only turn
# (no announce). It must NOT flip `composed` in telemetry. It never writes the
# system message — that is leg 3's target, covered further down.
# --------------------------------------------------------------------------- #

_BANNER = "[agentalloy · build] out.md not yet produced · 1/2 sections (missing: B)"


def _last_user_content(captured: dict[str, Any]) -> str:
    """Return the content of the last user message (not the last message overall)."""
    sent = json.loads(captured["body"])
    # Walk backwards to find the last user message (the appended system message is last).
    for msg in reversed(sent["messages"]):
        if msg["role"] == "user":
            assert isinstance(msg["content"], str)
            return msg["content"]
    raise AssertionError("no user message found")


def _system_content(captured: dict[str, Any]) -> str:
    """Return the content of the FIRST system message (where AgentAlloy prose is merged)."""
    sent = json.loads(captured["body"])
    for msg in sent["messages"]:
        if msg["role"] == "system":
            return msg["content"]
    raise AssertionError("no system message found")


def _harness_system_content(captured: dict[str, Any]) -> str:
    """Return the content of the FIRST system message (the harness's)."""
    sent = json.loads(captured["body"])
    for msg in sent["messages"]:
        if msg["role"] == "system":
            return msg["content"]
    raise AssertionError("no system message found")


def test_banner_only_turn_injects_into_upstream_last_user(tmp_path: Path) -> None:
    (tmp_path / ".agentalloy").mkdir()
    captured: dict[str, Any] = {}
    app = _make_app(orchestrator=_orchestrator("SHOULD-NOT-APPEAR"), captured=captured)
    # should_compose=False → no workflow block; banner set → banner-only injection.
    signal = SignalResult(should_compose=False, phase="build", task="t", banner=_BANNER)
    with patch(_SIGNAL, return_value=signal), TestClient(app) as client:
        resp = client.post("/v1/chat/completions", json=_body(tmp_path))
    assert resp.status_code == 200
    content = _last_user_content(captured)
    assert _BANNER in content
    assert "BEGIN AGENTALLOY-BANNER" in content
    assert "SHOULD-NOT-APPEAR" not in content
    # System message byte-identical.
    assert _system_content(captured) == "SYSTEM-CACHED"


def test_banner_appended_after_workflow_block_upstream(tmp_path: Path) -> None:
    (tmp_path / ".agentalloy").mkdir()
    captured: dict[str, Any] = {}
    app = _make_app(orchestrator=_orchestrator("ORIENTATION-PROSE"), captured=captured)
    signal = SignalResult(
        should_compose=True,
        announce=True,
        phase="build",
        task="the real task",
        workflow_prose="operate like so",
        banner=_BANNER,
        pending_announce=("build", ["sess-1"]),
    )
    with patch(_SIGNAL, return_value=signal), TestClient(app) as client:
        resp = client.post("/v1/chat/completions", json=_body(tmp_path))
    assert resp.status_code == 200
    content = _last_user_content(captured)
    # Workflow prose is delivered via the system-message leg (leg 3), not the user
    # message (leg 1), so it does NOT appear in the user message content.
    # The composed block (ORIENTATION-PROSE), phase marker, and banner are present.
    assert "ORIENTATION-PROSE" in content
    assert "phase=build" in content
    assert "operate like so" not in content
    assert _BANNER in content
    assert content.rstrip().endswith("<!-- END AGENTALLOY-BANNER -->")
    assert content.count("BEGIN AGENTALLOY-BANNER") == 1
    # Leg 1's composed output stays out of the system message; this fixture sets no
    # `workflow_system_prose`, so leg 3 is silent and the system message is untouched.
    assert _system_content(captured) == "SYSTEM-CACHED"
    assert _announced_file(tmp_path) == "build\tsess-1"


# --------------------------------------------------------------------------- #
# Workflow prose on the system message (OpenAI surface) — leg 3.
#
# Fires on EVERY carrier turn, outside the `should_compose` guard and outside
# `apply_signal`, and commits no cadence marker. A "delivered once" record here
# would recreate the bug #499 fixed: the harness rebuilds each request from its own
# local history and never observes proxy mutations, so the prose would vanish from
# turn 2 on. That failure mode is INVISIBLE on a single turn, which is why the
# two-turn test below is the load-bearing one.
#
# NOTE for anyone adding coverage here: `evaluate_signal` is patched wholesale and
# `SignalResult` is hand-built, so a fixture that does not explicitly set
# `workflow_system_prose` leaves leg 3 inert. A green run of a fixture that forgot
# the field proves nothing.
# --------------------------------------------------------------------------- #

_PROSE = "# SDD — Build\nWork the design's task slices as written."


def _prose_signal(**over: Any) -> SignalResult:
    base: dict[str, Any] = {
        "should_compose": False,
        "phase": "build",
        "task": "t",
        "workflow_system_prose": _PROSE,
    }
    base.update(over)
    return SignalResult(**base)


def test_prose_lands_on_system_message(tmp_path: Path) -> None:
    (tmp_path / ".agentalloy").mkdir()
    captured: dict[str, Any] = {}
    app = _make_app(orchestrator=_orchestrator("SHOULD-NOT-APPEAR"), captured=captured)
    with patch(_SIGNAL, return_value=_prose_signal()), TestClient(app) as client:
        resp = client.post("/v1/chat/completions", json=_body(tmp_path))
    assert resp.status_code == 200
    system = _system_content(captured)
    # Harness's own system prompt and AgentAlloy prose merged into first system message.
    assert "SYSTEM-CACHED" in system
    assert _PROSE in system
    assert system.count('<agentalloy-instructions phase="build">') == 1
    assert system.count("</agentalloy-instructions>") == 1


def test_prose_fires_on_a_quiet_turn(tmp_path: Path) -> None:
    """should_compose=False, no banner: leg 3 still delivers (it is outside the guard)."""
    (tmp_path / ".agentalloy").mkdir()
    captured: dict[str, Any] = {}
    app = _make_app(orchestrator=_orchestrator("SHOULD-NOT-APPEAR"), captured=captured)
    with patch(_SIGNAL, return_value=_prose_signal(banner=None)), TestClient(app) as client:
        resp = client.post("/v1/chat/completions", json=_body(tmp_path))
    assert resp.status_code == 200
    assert _PROSE in _system_content(captured)
    # Nothing landed on the user message this turn.
    assert _last_user_content(captured) == "the real task"


def test_prose_repeats_byte_identically_across_turns(tmp_path: Path) -> None:
    """The deliver-once regression guard. Turn 1 passing proves nothing on its own.

    The harness rebuilds each request from its own history, so turn 2 sends the
    SAME body it sent on turn 1 — proxy mutations are never echoed back. If leg 3
    ever grows a cadence marker, turn 2 goes silent here and nowhere else.
    """
    (tmp_path / ".agentalloy").mkdir()
    captured: dict[str, Any] = {}
    app = _make_app(orchestrator=_orchestrator("SHOULD-NOT-APPEAR"), captured=captured)
    seen: list[str] = []
    with patch(_SIGNAL, return_value=_prose_signal(banner=_BANNER)), TestClient(app) as client:
        for _ in range(2):
            resp = client.post("/v1/chat/completions", json=_body(tmp_path))
            assert resp.status_code == 200
            seen.append(_system_content(captured))

    assert _PROSE in seen[0]
    assert seen[1] == seen[0]  # byte-identical within a phase
    assert seen[1].count('<agentalloy-instructions phase="build">') == 1


def test_prose_replaced_on_phase_transition(tmp_path: Path) -> None:
    (tmp_path / ".agentalloy").mkdir()
    captured: dict[str, Any] = {}
    app = _make_app(orchestrator=_orchestrator("SHOULD-NOT-APPEAR"), captured=captured)

    # Turn 1 at build, then turn 2 at qa — with the harness replaying the system
    # message the proxy produced on turn 1 (a codex-style harness that persists
    # what it was handed would do exactly this).
    with patch(_SIGNAL, return_value=_prose_signal()), TestClient(app) as client:
        client.post("/v1/chat/completions", json=_body(tmp_path))

    # Capture the merged system message from turn 1.
    sent = json.loads(captured["body"])
    merged_sys = next(m for m in sent["messages"] if m["role"] == "system")

    # Simulate harness replay: the harness sees the merged system message and replays it.
    replayed = _body(tmp_path)
    replayed["messages"][0] = merged_sys  # merged system message from turn 1
    qa_prose = "# SDD — QA\nProve the acceptance criteria."
    with (
        patch(_SIGNAL, return_value=_prose_signal(phase="qa", workflow_system_prose=qa_prose)),
        TestClient(app) as client,
    ):
        resp = client.post("/v1/chat/completions", json=replayed)
    assert resp.status_code == 200

    system = _system_content(captured)
    assert 'phase="build"' not in system
    assert _PROSE not in system
    assert qa_prose in system
    assert system.count('<agentalloy-instructions phase="qa">') == 1
    # Harness's own system prompt preserved in merged message.
    assert "SYSTEM-CACHED" in system


def test_composed_block_does_not_reach_the_system_message(tmp_path: Path) -> None:
    """Leg 1 and leg 3 carry DIFFERENT content to DIFFERENT messages.

    Before the split, `_inject_openai` wrote the composed output into both the last
    user message and the system message. Pin that it no longer does.
    """
    (tmp_path / ".agentalloy").mkdir()
    captured: dict[str, Any] = {}
    app = _make_app(orchestrator=_orchestrator("ORIENTATION-PROSE"), captured=captured)
    signal = _prose_signal(
        should_compose=True,
        announce=True,
        task="the real task",
        workflow_prose="operate like so",
        banner=_BANNER,
        pending_announce=("build", ["sess-1"]),
    )
    with patch(_SIGNAL, return_value=signal), TestClient(app) as client:
        resp = client.post("/v1/chat/completions", json=_body(tmp_path))
    assert resp.status_code == 200

    user = _last_user_content(captured)
    system = _system_content(captured)
    assert "ORIENTATION-PROSE" in user
    assert "ORIENTATION-PROSE" not in system
    assert _PROSE in system
    assert _PROSE not in user
    assert _BANNER in user
    assert _BANNER not in system
    # Leg 1 still burns its own cadence marker; legs 2 and 3 add none.
    assert _announced_file(tmp_path) == "build\tsess-1"


def test_creates_system_message_when_none_exists(tmp_path: Path) -> None:
    """When the harness sends no system message, leg 3 creates one with AgentAlloy prose.

    The new behavior (separate system message) creates a system message if none
    exists, rather than returning None (no-op). Other legs (banner in user message)
    still land normally.
    """
    (tmp_path / ".agentalloy").mkdir()
    captured: dict[str, Any] = {}
    app = _make_app(orchestrator=_orchestrator("SHOULD-NOT-APPEAR"), captured=captured)
    body = _body(tmp_path)
    body["messages"] = [{"role": "user", "content": "the real task"}]
    with patch(_SIGNAL, return_value=_prose_signal(banner=_BANNER)), TestClient(app) as client:
        resp = client.post("/v1/chat/completions", json=body)
    assert resp.status_code == 200
    sent = json.loads(captured["body"])
    roles = [m["role"] for m in sent["messages"]]
    # A system message was created with the prose
    assert "system" in roles
    # Check the system message content directly (not via json.dumps which escapes em-dash).
    sys_msg = next(m for m in sent["messages"] if m["role"] == "system")
    assert _PROSE in sys_msg["content"]
    # Banner still lands in the user message
    assert _BANNER in _last_user_content(captured)


def test_prose_leg_does_not_burn_the_announce_marker(tmp_path: Path) -> None:
    """No user message: leg 1 cannot deliver, so the cadence must stay unburned even
    though leg 3 successfully writes the system message."""
    (tmp_path / ".agentalloy").mkdir()
    captured: dict[str, Any] = {}
    app = _make_app(orchestrator=_orchestrator("ORIENTATION-PROSE"), captured=captured)
    signal = _prose_signal(
        should_compose=True,
        announce=True,
        workflow_prose="operate like so",
        pending_announce=("build", ["sess-1"]),
    )
    with patch(_SIGNAL, return_value=signal), TestClient(app) as client:
        resp = client.post("/v1/chat/completions", json=_no_user_body(tmp_path))
    assert resp.status_code == 200
    assert _PROSE in _system_content(captured)
    assert _announced_file(tmp_path) is None


# --------------------------------------------------------------------------- #
# Cross-cutting guards (§D of the test plan).
# --------------------------------------------------------------------------- #


def test_pause_mode_and_non_carrier_turns_stay_silent(tmp_path: Path) -> None:
    """D1/D2: no phase (pause mode) and no prose (non-carrier) → nothing injected.

    `workflow_system_prose` is populated upstream in `evaluate_signal`, which leaves it
    None outside a governed phase. Leg 3's guard must respect BOTH halves of that:
    prose set but `phase is None` is also silent, since there is no phase to stamp.
    """
    (tmp_path / ".agentalloy").mkdir()
    for signal in (
        SignalResult(should_compose=False),  # pause mode: no phase, no prose
        SignalResult(should_compose=False, phase="build", task="t"),  # carrier, no prose
        SignalResult(should_compose=False, phase=None, workflow_system_prose=_PROSE),
    ):
        captured: dict[str, Any] = {}
        app = _make_app(orchestrator=_orchestrator("SHOULD-NOT-APPEAR"), captured=captured)
        with patch(_SIGNAL, return_value=signal), TestClient(app) as client:
            resp = client.post("/v1/chat/completions", json=_body(tmp_path))
        assert resp.status_code == 200
        assert json.loads(captured["body"])["messages"] == _body(tmp_path)["messages"]


def test_prose_leg_failure_does_not_cost_the_other_legs(tmp_path: Path) -> None:
    """D3: leg 3 is ordered last and try/except-wrapped, so a raising helper degrades
    to "no prose this turn" rather than dropping the banner or the composed block."""
    (tmp_path / ".agentalloy").mkdir()
    captured: dict[str, Any] = {}
    app = _make_app(orchestrator=_orchestrator("ORIENTATION-PROSE"), captured=captured)
    signal = _prose_signal(
        should_compose=True,
        announce=True,
        task="the real task",
        workflow_prose="operate like so",
        banner=_BANNER,
        pending_announce=("build", ["sess-1"]),
    )
    with (
        patch(_SIGNAL, return_value=signal),
        patch(
            "agentalloy.api.proxy_router.inject_into_openai_system_prompt",
            side_effect=RuntimeError("boom"),
        ),
        TestClient(app) as client,
    ):
        resp = client.post("/v1/chat/completions", json=_body(tmp_path))
    assert resp.status_code == 200
    user = _last_user_content(captured)
    assert "ORIENTATION-PROSE" in user
    assert _BANNER in user
    assert _system_content(captured) == "SYSTEM-CACHED"


def test_no_anthropic_cache_keys_on_the_openai_wire(tmp_path: Path) -> None:
    """D4: `cache_control`/ttl/breakpoints are an Anthropic-only concern. OpenAI does
    implicit prefix caching with no knob, so 3cb8ac1's ttl mirroring has no analog here
    and must not leak into the forwarded body."""
    (tmp_path / ".agentalloy").mkdir()
    captured: dict[str, Any] = {}
    app = _make_app(orchestrator=_orchestrator("ORIENTATION-PROSE"), captured=captured)
    signal = _prose_signal(
        should_compose=True,
        announce=True,
        task="the real task",
        workflow_prose="operate like so",
        banner=_BANNER,
        pending_announce=("build", ["sess-1"]),
    )
    with patch(_SIGNAL, return_value=signal), TestClient(app) as client:
        resp = client.post("/v1/chat/completions", json=_body(tmp_path))
    assert resp.status_code == 200
    body = captured["body"].decode()
    assert "cache_control" not in body
    assert '"ttl"' not in body
    assert "ephemeral" not in body
