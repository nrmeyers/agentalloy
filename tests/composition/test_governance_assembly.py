"""NXS-772: governance/system fragments surfaced through compose.

v5.4: assembly LLM is gone. Output is concatenated raw fragment text in a
deterministic order (system fragments first, then domain). These tests verify
the orchestrator integration: system_fragments appear in the response, the
output prefixes the system block, and EmptyResult preserves system_fragments.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentalloy.api.compose_models import ComposeRequest
from agentalloy.api.compose_router import get_orchestrator
from agentalloy.orchestration.compose import ComposeOrchestrator
from agentalloy.reads.models import ActiveFragment
from agentalloy.retrieval.domain import RetrievalResult
from agentalloy.retrieval.system import SystemRetrievalResult


def _frag(
    fid: str,
    ftype: str,
    content: str = "",
    skill: str = "sk-a",
    skill_class: str = "domain",
) -> ActiveFragment:
    return ActiveFragment(
        fragment_id=fid,
        fragment_type=ftype,
        sequence=1,
        content=content or f"content of {fid}",
        skill_id=skill,
        version_id=f"{skill}-v1",
        skill_class=skill_class,  # type: ignore[arg-type]
        category="design",
        domain_tags=[],
    )


_EMPTY_SYSTEM = SystemRetrievalResult(candidates=[], applied_skill_ids=[], retrieval_ms=0)


class _FakeOrchestrator(ComposeOrchestrator):
    """Stub orchestrator that short-circuits store + embed client."""

    def __init__(
        self,
        domain: RetrievalResult,
        system: SystemRetrievalResult,
    ) -> None:
        from agentalloy.telemetry.writer import NullTelemetryWriter

        self._domain = domain
        self._system = system
        self._embedding_model = "fake-embed"
        self._telemetry = NullTelemetryWriter()

    async def retrieve(self, req: ComposeRequest) -> RetrievalResult:  # noqa: ARG002
        return self._domain

    async def retrieve_system(self, req: ComposeRequest) -> SystemRetrievalResult:  # noqa: ARG002
        return self._system


def _install(app: FastAPI, orchestrator: ComposeOrchestrator) -> None:
    app.dependency_overrides[get_orchestrator] = lambda: orchestrator


def test_system_fragments_surfaced_in_composed_result(app: FastAPI, client: TestClient) -> None:
    sys_frag = _frag("sys-1", "guardrail", "GOV_RULE", skill="g", skill_class="system")
    domain_frag = _frag("d1", "execution", "DOMAIN_STEP")
    orchestrator = _FakeOrchestrator(
        domain=RetrievalResult(candidates=[domain_frag], eligible_count=1, retrieval_ms=10),
        system=SystemRetrievalResult(
            candidates=[sys_frag], applied_skill_ids=["g"], retrieval_ms=5
        ),
    )
    _install(app, orchestrator)
    body = client.post("/compose", json={"task": "t", "phase": "build"}).json()
    assert body["result_type"] == "composed"
    assert body["system_fragments"] == ["sys-1"]
    assert body["system_skills_applied"] is True
    # System fragments render before domain fragments in the output.
    output = body["output"]
    assert "GOV_RULE" in output
    assert "DOMAIN_STEP" in output
    assert output.find("GOV_RULE") < output.find("DOMAIN_STEP")


def test_system_skills_applied_false_when_no_system_fragments(
    app: FastAPI, client: TestClient
) -> None:
    domain_frag = _frag("d1", "execution")
    orchestrator = _FakeOrchestrator(
        domain=RetrievalResult(candidates=[domain_frag], eligible_count=1, retrieval_ms=10),
        system=_EMPTY_SYSTEM,
    )
    _install(app, orchestrator)
    body = client.post("/compose", json={"task": "t", "phase": "build"}).json()
    assert body["system_skills_applied"] is False
    assert body["system_fragments"] == []


def test_empty_result_includes_system_fragments_when_no_domain_matches(
    app: FastAPI, client: TestClient
) -> None:
    sys_frag = _frag("sys-1", "guardrail", skill="g", skill_class="system")
    orchestrator = _FakeOrchestrator(
        domain=RetrievalResult(candidates=[], eligible_count=0, retrieval_ms=5),
        system=SystemRetrievalResult(
            candidates=[sys_frag], applied_skill_ids=["g"], retrieval_ms=5
        ),
    )
    _install(app, orchestrator)
    body = client.post("/compose", json={"task": "t", "phase": "build"}).json()
    assert body["result_type"] == "empty"
    assert body["system_fragments"] == ["sys-1"]
    assert body["system_skills_applied"] is True
