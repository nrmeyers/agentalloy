"""Free-flow mode — pause workflow steering, keep skill composition.

Covers the phase-file ``mode``/``free_since`` round-trip (back/forward
compatible with the workflow-mode format), the compose-only guard in
``evaluate_signal`` (no orientation / banner / gates / intake, domain compose
kept, once-per-session cadence, daily reminder), and the hermetic proxy e2e on
both surfaces (native Anthropic passthrough + OpenAI chat-completions): domain
fragments are injected while every workflow-steering artifact is absent, and
the consolidated telemetry row is tagged ``category='free-flow'``.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock
from unittest.mock import MagicMock

import httpx
from fastapi.testclient import TestClient

from agentalloy.api.anthropic_passthrough import AnthropicPassthroughClient
from agentalloy.api.compose_models import ComposedResult, LatencyBreakdown
from agentalloy.api.proxy_context import encode_proj_token
from agentalloy.api.proxy_models import ProxyMessage, ProxyRequest
from agentalloy.api.proxy_signal import commit_markers, evaluate_signal
from agentalloy.app import create_app
from agentalloy.orchestration.compose import ComposeOrchestrator
from agentalloy.signals.skill_loader import (  # pyright: ignore[reportPrivateUsage]
    _read_phase,
    _write_phase_atomic,
    read_flow_state,
)
from agentalloy.storage.telemetry_store import open_telemetry_store

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_phase_file(
    tmp_path: Path,
    phase: str,
    *,
    free: bool = False,
    free_since: str | None = None,
) -> None:
    d = tmp_path / ".agentalloy"
    d.mkdir(exist_ok=True)
    lines = [f"phase: {phase}"]
    if free:
        lines.append("mode: free")
        lines.append(f'free_since: "{free_since or _iso(datetime.now(UTC))}"')
    (d / "phase").write_text("\n".join(lines) + "\n")


def _req(*user_texts: str, tools: bool = True) -> ProxyRequest:
    return ProxyRequest(
        model="gpt-4",
        messages=[ProxyMessage(role="user", content=t) for t in user_texts],
        tools=[{"name": "Read", "description": "read a file", "input_schema": {}}]
        if tools
        else None,
    )


def _eval(req: ProxyRequest, tmp_path: Path, session_id: str | None = None, *, mutate: bool = True):
    """Run the real ``evaluate_signal``; the workflow-skill loader is fenced so a
    free-mode request that wrongly falls through to the workflow path fails loudly."""

    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("workflow skill loader must not run in free-flow mode")

    with (
        mock.patch("agentalloy.api.proxy_signal._load_workflow_skill_for_phase", _boom),
        mock.patch("agentalloy.api.proxy_signal.check_transition_trigger", return_value=None),
    ):
        return asyncio.run(evaluate_signal(req, tmp_path, session_id=session_id, mutate=mutate))


def _orchestrator(output: str, captured_reqs: list[Any] | None = None) -> ComposeOrchestrator:
    m = MagicMock(spec=ComposeOrchestrator)

    async def compose(req: Any, **_kwargs: object) -> ComposedResult:
        if captured_reqs is not None:
            captured_reqs.append(req)
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

    m.compose = compose
    return m


# ---------------------------------------------------------------------------
# Phase-file round-trip: mode / free_since / back-compat
# ---------------------------------------------------------------------------


class TestPhaseFileFlowState:
    def test_old_file_without_mode_reads_workflow(self, tmp_path: Path) -> None:
        _write_phase_file(tmp_path, "build")
        assert read_flow_state(tmp_path) == ("workflow", None)
        assert _read_phase(tmp_path) == "build"

    def test_absent_file_reads_workflow(self, tmp_path: Path) -> None:
        assert read_flow_state(tmp_path) == ("workflow", None)

    def test_legacy_bare_phase_file(self, tmp_path: Path) -> None:
        d = tmp_path / ".agentalloy"
        d.mkdir()
        (d / "phase").write_text("build\n")
        assert read_flow_state(tmp_path) == ("workflow", None)
        assert _read_phase(tmp_path) == "build"

    def test_free_file_round_trip(self, tmp_path: Path) -> None:
        _write_phase_file(tmp_path, "design", free=True, free_since="2026-07-01T00:00:00Z")
        assert read_flow_state(tmp_path) == ("free", "2026-07-01T00:00:00Z")
        # An old agentalloy reading the new file must still see the phase (the
        # tolerant YAML reader in skill_loader is the oldest parser in the tree).
        assert _read_phase(tmp_path) == "design"

    def test_write_phase_atomic_preserves_free_flow_fields(self, tmp_path: Path) -> None:
        _write_phase_file(tmp_path, "build", free=True, free_since="2026-07-01T00:00:00Z")
        _write_phase_atomic(tmp_path, "qa")
        raw = (tmp_path / ".agentalloy" / "phase").read_text()
        assert raw.splitlines()[0] == "phase: qa"  # phase line stays first
        assert read_flow_state(tmp_path) == ("free", "2026-07-01T00:00:00Z")

    def test_write_phase_atomic_without_mode_stays_clean(self, tmp_path: Path) -> None:
        _write_phase_file(tmp_path, "build")
        _write_phase_atomic(tmp_path, "qa")
        assert (tmp_path / ".agentalloy" / "phase").read_text() == "phase: qa\n"

    def test_phase_set_cli_preserves_free_flow_fields(self, tmp_path: Path) -> None:
        from agentalloy.install.subcommands.flow import run_flow_free
        from agentalloy.install.subcommands.phase import run_phase_set

        run_phase_set("build", root=tmp_path, force=True)
        run_flow_free(root=tmp_path)
        result = run_phase_set("qa", root=tmp_path, force=True)
        assert result["phase"] == "qa"
        mode, since = read_flow_state(tmp_path)
        assert mode == "free" and since


# ---------------------------------------------------------------------------
# Signal layer: compose-only handling in free mode
# ---------------------------------------------------------------------------


class TestEvaluateSignalFreeFlow:
    def test_free_mode_composes_domain_only(self, tmp_path: Path) -> None:
        _write_phase_file(tmp_path, "build", free=True)
        r = _eval(_req("poke around the auth code"), tmp_path, session_id="s1")
        assert r.free_mode is True
        assert r.should_compose is True
        assert r.announce is True  # drives the task-keyed domain leg
        # Every workflow-steering channel is suppressed.
        assert r.workflow_prose is None
        assert r.banner is None
        assert r.advisories == []
        assert r.gates_met == [] and r.gates_unmet == []
        assert r.current_contract is None and r.announce_cursor is False
        # Phase preserved, cadence deferred under the free sentinel.
        assert r.phase == "build"
        assert r.pending_announce is not None and r.pending_announce[0] == "__free__"

    def test_mode_absent_is_not_free(self, tmp_path: Path) -> None:
        _write_phase_file(tmp_path, "build")
        with (
            mock.patch(
                "agentalloy.api.proxy_signal._load_workflow_skill_for_phase",
                return_value=None,
            ),
            mock.patch("agentalloy.api.proxy_signal.check_transition_trigger", return_value=None),
        ):
            r = asyncio.run(evaluate_signal(_req("task"), tmp_path, session_id="s1"))
        assert r.free_mode is False

    def test_carrier_gate_holds_in_free_mode(self, tmp_path: Path) -> None:
        _write_phase_file(tmp_path, "build", free=True)
        r = _eval(_req("background ping", tools=False), tmp_path, session_id="s1")
        assert r.should_compose is False and r.free_mode is True

    def test_once_per_session_cadence(self, tmp_path: Path) -> None:
        _write_phase_file(tmp_path, "build", free=True)
        first = _eval(_req("task"), tmp_path, session_id="s1")
        assert first.should_compose
        commit_markers(tmp_path, first, announce_emitted=True, cursor_emitted=False)
        second = _eval(_req("task", "more"), tmp_path, session_id="s1")
        assert second.should_compose is False
        # ... but a NEW session still composes.
        third = _eval(_req("other task"), tmp_path, session_id="s2")
        assert third.should_compose is True

    def test_no_phase_transition_written(self, tmp_path: Path) -> None:
        _write_phase_file(tmp_path, "build", free=True)
        _eval(_req("this is complete, ship it, all done"), tmp_path, session_id="s1")
        assert _read_phase(tmp_path) == "build"

    # -- intake ------------------------------------------------------------

    def test_free_before_intake_skips_intake(self, tmp_path: Path) -> None:
        _write_phase_file(tmp_path, "intake", free=True)
        r = _eval(_req("just exploring"), tmp_path, session_id="s1")
        # No intake orientation is composed; only the domain leg.
        assert r.free_mode is True and r.workflow_prose is None
        commit_markers(tmp_path, r, announce_emitted=True, cursor_emitted=False)

    def test_resume_runs_intake_as_first_request(self, tmp_path: Path) -> None:
        _write_phase_file(tmp_path, "intake", free=True)
        r = _eval(_req("just exploring"), tmp_path, session_id="s1")
        commit_markers(tmp_path, r, announce_emitted=True, cursor_emitted=False)
        # Resume: mode cleared, phase untouched.
        from agentalloy.install.subcommands.flow import run_flow_resume

        result = run_flow_resume(root=tmp_path)
        assert result == {"phase": "intake", "mode": "workflow", "changed": True}
        skill = {
            "skill_id": "sdd-intake",
            "signal_keywords": [],
            "exit_gates": {},
            "raw_prose": "INTAKE-ORIENTATION",
        }
        with (
            mock.patch(
                "agentalloy.api.proxy_signal._load_workflow_skill_for_phase",
                return_value=skill,
            ),
            mock.patch("agentalloy.api.proxy_signal.check_transition_trigger", return_value=None),
        ):
            after = asyncio.run(evaluate_signal(_req("real task"), tmp_path, session_id="s1"))
        assert after.free_mode is False
        assert after.announce is True  # intake orients as if first request
        assert after.workflow_prose == "INTAKE-ORIENTATION"

    # -- daily reminder ------------------------------------------------------

    def test_reminder_fires_after_24h_and_stamps_marker(self, tmp_path: Path) -> None:
        since = _iso(datetime.now(UTC) - timedelta(hours=25))
        _write_phase_file(tmp_path, "build", free=True, free_since=since)
        r = _eval(_req("task"), tmp_path, session_id="s1")
        assert r.reminder is not None
        assert "workflow paused (free-flow)" in r.reminder
        assert since[:10] in r.reminder
        assert "agentalloy flow resume" in r.reminder
        assert (tmp_path / ".agentalloy" / "free-reminded").exists()
        # Same day, second request: no reminder.
        again = _eval(_req("task two"), tmp_path, session_id="s1")
        assert again.reminder is None

    def test_reminder_quiet_within_24h(self, tmp_path: Path) -> None:
        _write_phase_file(tmp_path, "build", free=True)  # free_since = now
        r = _eval(_req("task"), tmp_path, session_id="s1")
        assert r.reminder is None
        assert not (tmp_path / ".agentalloy" / "free-reminded").exists()

    def test_reminder_not_stamped_when_read_only(self, tmp_path: Path) -> None:
        since = _iso(datetime.now(UTC) - timedelta(hours=48))
        _write_phase_file(tmp_path, "build", free=True, free_since=since)
        r = _eval(_req("task"), tmp_path, session_id="s1", mutate=False)
        assert r.reminder is not None
        assert not (tmp_path / ".agentalloy" / "free-reminded").exists()

    def test_resume_clears_reminder_marker(self, tmp_path: Path) -> None:
        since = _iso(datetime.now(UTC) - timedelta(hours=25))
        _write_phase_file(tmp_path, "build", free=True, free_since=since)
        _eval(_req("task"), tmp_path, session_id="s1")
        assert (tmp_path / ".agentalloy" / "free-reminded").exists()
        from agentalloy.install.subcommands.flow import run_flow_resume

        run_flow_resume(root=tmp_path)
        assert not (tmp_path / ".agentalloy" / "free-reminded").exists()


# ---------------------------------------------------------------------------
# Proxy e2e — native Anthropic passthrough surface
# ---------------------------------------------------------------------------

_STEERING_NEEDLES = (
    "AGENTALLOY-BANNER",  # per-turn phase banner
    "[agentalloy-eval]",  # gate advisories
    "[agentalloy ·",  # banner body ("[agentalloy · <phase>] MUST ...")
    "INTAKE-ORIENTATION",
)


def _anthropic_body(text: str = "the real task") -> dict[str, Any]:
    return {
        "model": "claude-test",
        "max_tokens": 100,
        "system": "SYSTEM-CACHED-BLOCK",
        "messages": [{"role": "user", "content": text}],
        "tools": [{"name": "Read", "description": "read", "input_schema": {}}],
        "stream": False,
    }


def _anthropic_upstream(captured: dict[str, Any]) -> httpx.AsyncClient:
    async def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(
            200,
            json={"type": "message", "id": "msg_1", "role": "assistant", "content": []},
            request=request,
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _passthrough_app(
    captured: dict[str, Any],
    orchestrator: ComposeOrchestrator,
    telemetry_store: Any = None,
) -> Any:
    app = create_app(use_default_lifespan=False)
    app.state.anthropic_passthrough_client = AnthropicPassthroughClient(
        upstream_base_url="http://mock-upstream",
        client=_anthropic_upstream(captured),
    )
    app.state.embed_client = MagicMock()
    app.state.telemetry_store = telemetry_store if telemetry_store is not None else MagicMock()
    from agentalloy.api.compose_router import get_orchestrator

    app.dependency_overrides[get_orchestrator] = lambda: orchestrator
    return app


class TestPassthroughSurfaceFreeFlow:
    def test_domain_injected_without_steering(self, tmp_path: Path) -> None:
        _write_phase_file(tmp_path, "build", free=True)
        captured: dict[str, Any] = {}
        reqs: list[Any] = []
        store = open_telemetry_store(tmp_path / "telemetry.duck")
        app = _passthrough_app(captured, _orchestrator("DOMAIN-SKILL-PROSE", reqs), store)
        with TestClient(app) as client:
            resp = client.post(
                f"/proj/{encode_proj_token(tmp_path)}/v1/messages", json=_anthropic_body()
            )
        assert resp.status_code == 200
        forwarded = captured["body"].decode("utf-8")
        # Domain fragments injected...
        assert "DOMAIN-SKILL-PROSE" in forwarded
        # ...via a domain-only compose keyed on the task text...
        assert len(reqs) == 1
        assert reqs[0].legs == "domain"
        assert reqs[0].task == "the real task"
        # ...and no workflow steering of any kind.
        for needle in _STEERING_NEEDLES:
            assert needle not in forwarded
        # Consolidated trace row: composed + tagged free-flow.
        rows = store.query_traces()
        store.close()
        assert len(rows) == 1
        assert rows[0].status == "proxy_composed"
        assert rows[0].category == "free-flow"
        assert rows[0].phase == "build"

    def test_reminder_line_rides_the_injection(self, tmp_path: Path) -> None:
        since = _iso(datetime.now(UTC) - timedelta(hours=30))
        _write_phase_file(tmp_path, "build", free=True, free_since=since)
        captured: dict[str, Any] = {}
        app = _passthrough_app(captured, _orchestrator("DOMAIN-SKILL-PROSE"))
        token = encode_proj_token(tmp_path)
        with TestClient(app) as client:
            client.post(f"/proj/{token}/v1/messages", json=_anthropic_body())
            forwarded_first = captured["body"].decode("utf-8")
            # Second request, same session fingerprint burnt + reminder stamped:
            client.post(f"/proj/{token}/v1/messages", json=_anthropic_body())
            forwarded_second = captured["body"].decode("utf-8")
        assert "workflow paused (free-flow)" in forwarded_first
        assert (tmp_path / ".agentalloy" / "free-reminded").exists()
        assert "workflow paused (free-flow)" not in forwarded_second

    def test_workflow_mode_unchanged(self, tmp_path: Path) -> None:
        """Guard: without ``mode: free`` the workflow path still orients."""
        _write_phase_file(tmp_path, "build")
        captured: dict[str, Any] = {}
        skill = {
            "skill_id": "sdd-build",
            "signal_keywords": [],
            "exit_gates": {},
            "raw_prose": "BUILD-ORIENTATION",
        }
        app = _passthrough_app(captured, _orchestrator("DOMAIN-SKILL-PROSE"))
        with (
            mock.patch(
                "agentalloy.api.proxy_signal._load_workflow_skill_for_phase",
                return_value=skill,
            ),
            mock.patch("agentalloy.api.proxy_signal.check_transition_trigger", return_value=None),
            TestClient(app) as client,
        ):
            client.post(f"/proj/{encode_proj_token(tmp_path)}/v1/messages", json=_anthropic_body())
        forwarded = captured["body"].decode("utf-8")
        assert "BUILD-ORIENTATION" in forwarded
        assert "AGENTALLOY-BANNER" in forwarded


# ---------------------------------------------------------------------------
# Proxy e2e — OpenAI chat-completions surface
# ---------------------------------------------------------------------------


def _openai_upstream(captured: dict[str, Any]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
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


class TestOpenAISurfaceFreeFlow:
    def test_domain_injected_without_steering(self, tmp_path: Path) -> None:
        _write_phase_file(tmp_path, "build", free=True)
        captured: dict[str, Any] = {}
        store = open_telemetry_store(tmp_path / "telemetry.duck")
        app = create_app(use_default_lifespan=False)
        app.state.upstream_client = _openai_upstream(captured)
        app.state.embed_client = MagicMock()
        app.state.telemetry_store = store
        from agentalloy.api.compose_router import get_orchestrator

        app.dependency_overrides[get_orchestrator] = lambda: _orchestrator("DOMAIN-SKILL-PROSE")
        body = {
            "model": "gpt-4",
            "messages": [
                {"role": "system", "content": "SYSTEM-CACHED"},
                {"role": "user", "content": "the real task"},
            ],
            "metadata": {"cwd": str(tmp_path)},
            "tools": [{"type": "function", "function": {"name": "read"}}],
        }
        with TestClient(app) as client:
            resp = client.post("/v1/chat/completions", json=body)
        assert resp.status_code == 200
        forwarded = json.loads(captured["body"])
        user_texts = [m["content"] for m in forwarded["messages"] if m["role"] == "user"]
        joined = json.dumps(user_texts)
        assert "DOMAIN-SKILL-PROSE" in joined
        for needle in _STEERING_NEEDLES:
            assert needle not in joined
        # System message untouched.
        assert forwarded["messages"][0]["content"] == "SYSTEM-CACHED"
        rows = store.query_traces()
        store.close()
        assert len(rows) == 1
        assert rows[0].status == "proxy_composed"
        assert rows[0].category == "free-flow"
