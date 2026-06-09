"""AC-1..3 for the compose handler (NXS-768).

Substitutes a ``_FakeOrchestrator`` that overrides ``retrieve`` and
``retrieve_system`` so the handler's response mapping is exercised without
hitting LadybugDB or FastFlowLM.

v5.4: assembly stage is gone. The orchestrator builds ``output`` from raw
fragment content; no LLM is invoked.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentalloy.api.compose_models import ComposeRequest
from agentalloy.api.compose_router import get_orchestrator
from agentalloy.orchestration.compose import (
    ComposeOrchestrator,
    RetrievalStageError,
)
from agentalloy.reads.models import ActiveFragment
from agentalloy.retrieval.domain import RetrievalResult
from agentalloy.retrieval.system import SystemRetrievalResult

_EMPTY_SYSTEM = SystemRetrievalResult(candidates=[], applied_skill_ids=[], retrieval_ms=0)


def _fake_fragment(fid: str, ftype: str = "execution", skill: str = "sk-a") -> ActiveFragment:
    return ActiveFragment(
        fragment_id=fid,
        fragment_type=ftype,
        sequence=1,
        content=f"content of {fid}",
        skill_id=skill,
        version_id=f"{skill}-v2",
        skill_class="domain",
        category="design",
        domain_tags=["python"],
    )


RetrieveFn = Callable[[ComposeRequest], RetrievalResult]


class _FakeOrchestrator(ComposeOrchestrator):
    """Overrides retrieve + retrieve_system with test doubles. The compose()
    method itself is unchanged — it formats fragments deterministically."""

    def __init__(
        self,
        retrieve_fn: RetrieveFn,
        system_result: SystemRetrievalResult = _EMPTY_SYSTEM,
    ) -> None:
        from agentalloy.telemetry.writer import NullTelemetryWriter

        self._retrieve_fn = retrieve_fn
        self._system_result = system_result
        self._embedding_model = "fake-embed"
        self._telemetry = NullTelemetryWriter()

    async def retrieve(self, req: ComposeRequest) -> RetrievalResult:  # noqa: ARG002
        return self._retrieve_fn(req)

    async def retrieve_system(self, req: ComposeRequest) -> SystemRetrievalResult:  # noqa: ARG002
        return self._system_result


def _install_orchestrator(app: FastAPI, orchestrator: ComposeOrchestrator) -> None:
    app.dependency_overrides[get_orchestrator] = lambda: orchestrator


# -------- AC-1: composed success --------


def test_compose_returns_composed_result(app: FastAPI, client: TestClient) -> None:
    frags = [_fake_fragment("f1"), _fake_fragment("f2", skill="sk-b")]

    def retrieve(_req: ComposeRequest) -> RetrievalResult:
        return RetrievalResult(candidates=frags, eligible_count=5, retrieval_ms=120)

    _install_orchestrator(app, _FakeOrchestrator(retrieve))
    resp = client.post("/compose", json={"task": "build a thing", "phase": "design"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["result_type"] == "composed"
    assert "content of f1" in body["output"]
    assert "content of f2" in body["output"]
    assert body["domain_fragments"] == ["f1", "f2"]
    assert body["source_skills"] == ["sk-a", "sk-b"]
    assert body["system_skills_applied"] is False
    assert body["assembly_tier"] == 0
    assert body["latency_ms"]["retrieval_ms"] == 120
    assert body["latency_ms"]["assembly_ms"] == 0


def test_compose_source_skills_dedup_preserves_first_appearance(
    app: FastAPI, client: TestClient
) -> None:
    frags = [
        _fake_fragment("f1", skill="sk-b"),
        _fake_fragment("f2", skill="sk-a"),
        _fake_fragment("f3", skill="sk-b"),
    ]

    def retrieve(_req: ComposeRequest) -> RetrievalResult:
        return RetrievalResult(candidates=frags, eligible_count=3, retrieval_ms=1)

    _install_orchestrator(app, _FakeOrchestrator(retrieve))
    body = client.post("/compose", json={"task": "t", "phase": "design"}).json()
    assert body["source_skills"] == ["sk-b", "sk-a"]  # dedup preserves first appearance


# -------- AC-2: empty result --------


def test_compose_returns_empty_result_on_no_candidates(app: FastAPI, client: TestClient) -> None:
    def retrieve(_req: ComposeRequest) -> RetrievalResult:
        return RetrievalResult(candidates=[], eligible_count=0, retrieval_ms=50)

    _install_orchestrator(app, _FakeOrchestrator(retrieve))
    resp = client.post("/compose", json={"task": "t", "phase": "design"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["result_type"] == "empty"
    assert body["output"] == ""
    assert body["reason"] == "no_domain_fragments_matched"


# -------- AC-3: retrieval-stage failure -> 503 --------


def test_retrieval_stage_error_maps_to_503(app: FastAPI, client: TestClient) -> None:
    def retrieve(_req: ComposeRequest) -> RetrievalResult:
        raise RetrievalStageError("embedding_failed", "embed down", available=None)

    _install_orchestrator(app, _FakeOrchestrator(retrieve))
    resp = client.post("/compose", json={"task": "t", "phase": "design"})
    assert resp.status_code == 503
    body = resp.json()
    assert body["stage"] == "retrieval"
    assert body["code"] == "embedding_failed"


def test_retrieval_store_unavailable(app: FastAPI, client: TestClient) -> None:
    def retrieve(_req: ComposeRequest) -> RetrievalResult:
        raise RetrievalStageError("store_unavailable", "ladybug down", available=None)

    _install_orchestrator(app, _FakeOrchestrator(retrieve))
    resp = client.post("/compose", json={"task": "t", "phase": "design"})
    assert resp.status_code == 503
    assert resp.json()["code"] == "store_unavailable"


def test_retrieval_embedding_model_unavailable(app: FastAPI, client: TestClient) -> None:
    def retrieve(_req: ComposeRequest) -> RetrievalResult:
        raise RetrievalStageError(
            "embedding_model_unavailable", "qwen3-embedding not loaded", available=None
        )

    _install_orchestrator(app, _FakeOrchestrator(retrieve))
    resp = client.post("/compose", json={"task": "t", "phase": "design"})
    assert resp.status_code == 503
    body = resp.json()
    assert body["stage"] == "retrieval"
    assert body["code"] == "embedding_model_unavailable"
