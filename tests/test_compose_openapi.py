"""Confirm /compose is documented in the OpenAPI schema and validation still fires."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentalloy.api.compose_models import ComposeRequest, EmptyResult
from agentalloy.api.compose_router import get_orchestrator
from agentalloy.orchestration.compose import ComposeOrchestrator
from agentalloy.retrieval.domain import RetrievalResult


class _NoopOrchestrator(ComposeOrchestrator):
    """Orchestrator that returns empty for every call — used for validation-only tests."""

    def __init__(self) -> None:
        self._assembly_model = "noop"

    async def compose(self, req: ComposeRequest, **_kwargs: object) -> EmptyResult:
        return EmptyResult(
            task=req.task,
            phase=req.phase,
            system_fragments=[],
            system_skills_applied=False,
        )

    async def retrieve(self, req: ComposeRequest) -> RetrievalResult:  # noqa: ARG002
        return RetrievalResult(candidates=[], eligible_count=0, retrieval_ms=0)


def _install_noop(app: FastAPI) -> None:
    app.dependency_overrides[get_orchestrator] = lambda: _NoopOrchestrator()


def test_openapi_exposes_compose_route(client: TestClient) -> None:
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    schema: dict[str, Any] = resp.json()
    paths: dict[str, Any] = schema["paths"]
    assert "/compose" in paths
    assert "post" in paths["/compose"]


def test_openapi_documents_503_error_shape(client: TestClient) -> None:
    schema: dict[str, Any] = client.get("/openapi.json").json()
    compose_post: dict[str, Any] = schema["paths"]["/compose"]["post"]
    responses: dict[str, Any] = compose_post["responses"]
    assert "503" in responses


def test_compose_rejects_invalid_phase(app: FastAPI, client: TestClient) -> None:
    _install_noop(app)
    resp = client.post("/compose", json={"task": "test", "phase": "invalid"})
    assert resp.status_code == 422


def test_compose_rejects_missing_task(app: FastAPI, client: TestClient) -> None:
    _install_noop(app)
    resp = client.post("/compose", json={"phase": "design"})
    assert resp.status_code == 422


def test_compose_rejects_empty_task(app: FastAPI, client: TestClient) -> None:
    _install_noop(app)
    resp = client.post("/compose", json={"task": "", "phase": "design"})
    assert resp.status_code == 422
