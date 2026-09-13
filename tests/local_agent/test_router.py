"""LocalAgentRouter (M1d) — /local-agent/ask and /local-agent/health.

Pins the HTTP contract: request validation (question bounds, phase, repo_root),
the response shape on 200, the structured 503 body on an LM outage, the 400
details, and the trace-writer attribution rules. The LM is replaced with a
scripted client; the state store is a bare object (the validator only holds a
reference to it when no index action runs).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentalloy.api.state_router import get_state_store
from agentalloy.local_agent import router as router_module
from agentalloy.local_agent.client import ClientStageError
from agentalloy.local_agent.config import LocalAgentConfig, LocalAgentMode

# ---------------------------------------------------------------------------
# Fakes and fixtures
# ---------------------------------------------------------------------------


class _FakeStateStore:
    """Held by the Validator but never called (the scripts below run no
    index/state actions)."""


class _ScriptedClient:
    """Pops completions from ``script``; BaseException items are raised."""

    def __init__(self, script: list[object]) -> None:
        self._script = list(script)

    def chat(
        self, prompt, *, stage: str, max_tokens: int, response_format: dict | None = None
    ) -> tuple[str, float]:
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        assert item is not None
        return item, 3


class _RecordingTraceWriter:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def record(
        self, *, result: Any, question: str, phase: str | None = None, repo: str | None = None
    ) -> None:
        self.calls.append({"result": result, "question": question, "phase": phase, "repo": repo})


def _config() -> LocalAgentConfig:
    return LocalAgentConfig(
        mode=LocalAgentMode.ON,
        url="http://127.0.0.1:59999",
        model="test-model",
        timeout_ms=30000,
        max_steps=2,
        max_tokens=2048,
        result_cap_chars=2400,
    )


@pytest.fixture()
def app() -> FastAPI:
    application = FastAPI()
    application.state.code_index_state = None
    application.state.telemetry_querier = None
    application.state.compose_orchestrator = None
    application.state.local_agent_trace_writer = None
    application.include_router(router_module.router)
    application.dependency_overrides[get_state_store] = lambda: _FakeStateStore()
    return application


@pytest.fixture()
def client(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(router_module, "get_config", lambda: _config())
    monkeypatch.setattr(
        router_module,
        "default_repo_root",
        lambda: Path.cwd(),
    )  # isolate from AGENTALLOY_PROJECT_DIR
    return TestClient(app)


def _script_client(monkeypatch: pytest.MonkeyPatch, script: list[object]) -> None:
    def _make(config: Any) -> _ScriptedClient:
        return _ScriptedClient(script)

    monkeypatch.setattr(router_module, "OpenAICompatClient", _make)


def _attach_trace_writer(app: FastAPI) -> _RecordingTraceWriter:
    writer = _RecordingTraceWriter()
    app.state.local_agent_trace_writer = writer
    return writer


# ---------------------------------------------------------------------------
# POST /local-agent/ask
# ---------------------------------------------------------------------------


class TestAsk:
    def test_success_shape(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        _script_client(monkeypatch, ['{"action": "none"}', "Nothing to look up."])
        response = client.post("/local-agent/ask", json={"question": "where is the auth bug?"})

        assert response.status_code == 200
        body = response.json()
        assert set(body) == {
            "answer",
            "steps",
            "degraded",
            "degrade_reason",
            "stop_reason",
            "model_tag",
            "total_ms",
        }
        assert body["answer"] == "Nothing to look up."
        assert body["degraded"] is False
        assert body["degrade_reason"] is None
        assert body["stop_reason"] == "none"
        assert body["model_tag"] == "test-model"
        assert isinstance(body["total_ms"], int)
        assert len(body["steps"]) == 1
        step = body["steps"][0]
        assert set(step) == {
            "step",
            "action",
            "args",
            "validation",
            "result_chars",
            "stage_latencies",
        }
        assert (step["step"], step["action"], step["args"]) == (1, "none", {})

    def test_phase_forwarded(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        _script_client(monkeypatch, ['{"action": "none"}', "ok"])
        response = client.post(
            "/local-agent/ask", json={"question": "q", "repo": "main-repo", "phase": "build"}
        )
        assert response.status_code == 200
        assert response.json()["stop_reason"] == "none"

    def test_unknown_phase_400(self, client: TestClient) -> None:
        response = client.post("/local-agent/ask", json={"question": "q", "phase": "not-a-phase"})
        assert response.status_code == 400
        assert "unknown phase" in response.json()["detail"]
        assert "not-a-phase" in response.json()["detail"]

    def test_bad_repo_root_400(self, client: TestClient) -> None:
        response = client.post(
            "/local-agent/ask", json={"question": "q", "repo_root": "/nonexistent/xyz"}
        )
        assert response.status_code == 400
        assert "is not a directory" in response.json()["detail"]

    def test_empty_question_422(self, client: TestClient) -> None:
        response = client.post("/local-agent/ask", json={"question": ""})
        assert response.status_code == 422

    def test_question_too_long_422(self, client: TestClient) -> None:
        response = client.post("/local-agent/ask", json={"question": "x" * 8001})
        assert response.status_code == 422

    def test_lm_unavailable_503_structured(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _script_client(monkeypatch, [ClientStageError("classify", "connection refused")])
        response = client.post("/local-agent/ask", json={"question": "q"})

        assert response.status_code == 503
        assert response.json()["detail"] == {
            "code": "local_agent_unavailable",
            "stage": "classify",
            "reason": "connection refused",
        }


# ---------------------------------------------------------------------------
# Trace attribution
# ---------------------------------------------------------------------------


class TestTraceAttribution:
    def test_explicit_repo_wins(
        self, client: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _script_client(monkeypatch, ['{"action": "none"}', "ok"])
        writer = _attach_trace_writer(app)
        response = client.post(
            "/local-agent/ask",
            json={"question": "where is the auth bug?", "repo": "main-repo", "phase": "build"},
        )
        assert response.status_code == 200
        assert len(writer.calls) == 1
        assert writer.calls[0]["question"] == "where is the auth bug?"
        assert writer.calls[0]["phase"] == "build"
        assert writer.calls[0]["repo"] == "main-repo"

    def test_no_repo_no_root_is_default_repo(
        self, client: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _script_client(monkeypatch, ['{"action": "none"}', "ok"])
        writer = _attach_trace_writer(app)
        response = client.post("/local-agent/ask", json={"question": "q"})
        assert response.status_code == 200
        assert writer.calls[0]["repo"] is None
        assert writer.calls[0]["phase"] is None

    def test_named_root_slugges_to_repo_key(
        self, client: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _script_client(monkeypatch, ['{"action": "none"}', "ok"])
        monkeypatch.setattr(router_module, "_repo_key_for", lambda root: "tmp-slug")
        writer = _attach_trace_writer(app)
        response = client.post(
            "/local-agent/ask", json={"question": "q", "repo_root": str(tmp_path)}
        )
        assert response.status_code == 200
        assert writer.calls[0]["repo"] == "tmp-slug"

    def test_no_writer_is_a_noop(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        # app.state.local_agent_trace_writer is None by default in the fixture.
        _script_client(monkeypatch, ['{"action": "none"}', "ok"])
        response = client.post("/local-agent/ask", json={"question": "q"})
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# GET /local-agent/health
# ---------------------------------------------------------------------------


class TestHealth:
    def test_ok(self, client: TestClient) -> None:
        response = client.get("/local-agent/health")
        assert response.status_code == 200
        assert response.json() == {
            "status": "ok",
            "model": "test-model",
            "url": "http://127.0.0.1:59999",
        }

    def test_degraded_reports_latch_reason(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            router_module, "endpoint_down_reason", lambda: "connect timeout: localhost:59999"
        )
        response = client.get("/local-agent/health")
        body = response.json()
        assert body["status"] == "degraded"
        assert body["reason"] == "connect timeout: localhost:59999"
        assert body["model"] == "test-model"
        assert body["url"] == "http://127.0.0.1:59999"
