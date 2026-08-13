"""Two-tier injection: the ``legs`` control on ComposeRequest.

Tier 1 (phase-entry announce) composes ``legs="system"`` — system prose only,
domain suppressed, never an EmptyResult (the workflow prose is the payload, the
router prepends it). Tier 2 (per work-item) composes ``legs="domain"`` — domain
only, system suppressed, EmptyResult when nothing matches. ``legs="both"`` is the
direct ``/compose`` default and is unchanged.
"""

from __future__ import annotations

import asyncio

from agentalloy.api.compose_models import ComposeRequest
from agentalloy.orchestration.compose import ComposeOrchestrator
from agentalloy.reads.models import ActiveFragment
from agentalloy.retrieval.domain import RetrievalResult
from agentalloy.retrieval.system import SystemRetrievalResult


def _domain_frag(fid: str) -> ActiveFragment:
    return ActiveFragment(
        fragment_id=fid,
        fragment_type="execution",
        sequence=1,
        content=f"DOMAIN {fid}",
        skill_id="pytest-fixtures",
        version_id="pytest-fixtures-v1",
        skill_class="domain",
        category="build",
        domain_tags=["pytest"],
    )


def _system_frag(fid: str) -> ActiveFragment:
    return ActiveFragment(
        fragment_id=fid,
        fragment_type="governance",
        sequence=1,
        content=f"SYSTEM {fid}",
        skill_id="sys-ci",
        version_id="sys-ci-v1",
        skill_class="system",
        category="process",
        domain_tags=[],
    )


class _Spy(ComposeOrchestrator):
    """Records which legs were retrieved so we can assert suppression."""

    def __init__(self) -> None:
        from agentalloy.telemetry.writer import NullTelemetryWriter

        self._embedding_model = "fake"
        self._telemetry = NullTelemetryWriter()
        self.domain_called = False
        self.system_called = False

    async def retrieve(self, req: ComposeRequest) -> RetrievalResult:  # noqa: ARG002
        self.domain_called = True
        return RetrievalResult(candidates=[_domain_frag("d1")], eligible_count=1, retrieval_ms=1)

    async def retrieve_system(self, req: ComposeRequest) -> SystemRetrievalResult:  # noqa: ARG002
        self.system_called = True
        return SystemRetrievalResult(
            candidates=[_system_frag("s1")], applied_skill_ids=["sys-ci"], retrieval_ms=1
        )


def _compose(legs: str) -> tuple[_Spy, object]:
    spy = _Spy()
    req = ComposeRequest(task="implement the cache", phase="build", legs=legs)  # type: ignore[arg-type]
    result = asyncio.run(spy.compose(req))
    return spy, result


def test_both_legs_assembles_system_and_domain() -> None:
    spy, result = _compose("both")
    assert spy.domain_called and spy.system_called
    assert result.result_type == "composed"  # type: ignore[attr-defined]
    assert "SYSTEM s1" in result.output and "DOMAIN d1" in result.output  # type: ignore[attr-defined]


def test_system_leg_suppresses_domain_and_never_empties() -> None:
    spy, result = _compose("system")
    # Domain retrieval is skipped entirely (no wasted embed/rerank)...
    assert spy.domain_called is False
    assert spy.system_called is True
    # ...and a system-only compose is never EmptyResult even with no domain hits.
    assert result.result_type == "composed"  # type: ignore[attr-defined]
    assert "SYSTEM s1" in result.output  # type: ignore[attr-defined]
    assert "DOMAIN" not in result.output  # type: ignore[attr-defined]
    assert result.domain_fragments == []  # type: ignore[attr-defined]


def test_domain_leg_suppresses_system() -> None:
    spy, result = _compose("domain")
    assert spy.domain_called is True
    assert spy.system_called is False
    assert result.result_type == "composed"  # type: ignore[attr-defined]
    assert "DOMAIN d1" in result.output  # type: ignore[attr-defined]
    assert "SYSTEM" not in result.output  # type: ignore[attr-defined]


def test_domain_leg_empty_returns_empty_result() -> None:
    class _NoDomain(_Spy):
        async def retrieve(self, req: ComposeRequest) -> RetrievalResult:  # noqa: ARG002
            self.domain_called = True
            return RetrievalResult(candidates=[], eligible_count=0, retrieval_ms=1)

    spy = _NoDomain()
    req = ComposeRequest(task="nothing matches", phase="build", legs="domain")
    result = asyncio.run(spy.compose(req))
    assert result.result_type == "empty"  # type: ignore[attr-defined]
    assert spy.system_called is False
