"""NXS-783: compose telemetry instrumentation — TelemetryRecord written for every compose call.

v5.4: assembly stage is gone. Token counts (input_tokens / output_tokens) are
no longer populated since there's no LLM in the path.
"""

from __future__ import annotations

import pytest

from agentalloy.api.compose_models import ComposeRequest, Phase
from agentalloy.orchestration.compose import (
    ASSEMBLY_TIER,
    ComposeOrchestrator,
)
from agentalloy.retrieval.domain import RetrievalResult
from agentalloy.retrieval.embedding_errors import (
    EmbeddingError,
    EmbeddingErrorCode,
    EmbeddingErrorResult,
)
from agentalloy.retrieval.system import SystemRetrievalResult
from agentalloy.telemetry import TelemetryRecord
from agentalloy.telemetry.writer import NullTelemetryWriter
from tests.support import fake_fragment


class _RecordingWriter(NullTelemetryWriter):
    def __init__(self) -> None:
        self.records: list[TelemetryRecord] = []

    def write(self, record: TelemetryRecord) -> None:
        self.records.append(record)


class _FakeOrchestrator(ComposeOrchestrator):
    """Orchestrator with retrieve / retrieve_system stubbed out."""

    def __init__(
        self,
        domain: RetrievalResult | EmbeddingErrorResult,
        system: SystemRetrievalResult,
        writer: _RecordingWriter,
    ) -> None:
        self._domain = domain
        self._system = system
        self._embedding_model = "fake-embed"
        self._telemetry = writer

    async def retrieve(self, req: ComposeRequest) -> RetrievalResult | EmbeddingErrorResult:  # noqa: ARG002
        return self._domain

    async def retrieve_system(self, req: ComposeRequest) -> SystemRetrievalResult:  # noqa: ARG002
        return self._system


def _req(task: str = "write a handler", phase: Phase = "design") -> ComposeRequest:
    return ComposeRequest(task=task, phase=phase)


def _empty_system() -> SystemRetrievalResult:
    return SystemRetrievalResult(candidates=[], applied_skill_ids=[], retrieval_ms=0)


@pytest.mark.asyncio
async def test_requesting_agent_flows_to_record_on_success() -> None:
    """A compose with an origin tag records it on the success-path TelemetryRecord."""
    writer = _RecordingWriter()
    frag = fake_fragment("f1", "execution", skill="sk-a")
    domain = RetrievalResult(candidates=[frag], eligible_count=1, retrieval_ms=10)
    orch = _FakeOrchestrator(domain, _empty_system(), writer)
    await orch.compose(ComposeRequest(task="x", phase="design", requesting_agent="post_tool_use"))
    assert writer.records[-1].requesting_agent == "post_tool_use"


@pytest.mark.asyncio
async def test_requesting_agent_flows_to_record_on_empty() -> None:
    """The origin tag is recorded even when retrieval yields nothing (compose_empty)."""
    writer = _RecordingWriter()
    domain = RetrievalResult(candidates=[], eligible_count=0, retrieval_ms=5)
    orch = _FakeOrchestrator(domain, _empty_system(), writer)
    await orch.compose(ComposeRequest(task="x", phase="design", requesting_agent="post_tool_use"))
    assert writer.records[-1].requesting_agent == "post_tool_use"


@pytest.mark.asyncio
async def test_compose_writes_telemetry_record() -> None:
    writer = _RecordingWriter()
    frag = fake_fragment("f1", "execution", skill="sk-a")
    domain = RetrievalResult(candidates=[frag], eligible_count=1, retrieval_ms=10)
    orch = _FakeOrchestrator(domain, _empty_system(), writer)

    await orch.compose(_req())

    assert len(writer.records) == 1
    r = writer.records[0]
    assert r.result_type == "compose"
    assert r.task_prompt == "write a handler"
    assert r.phase == "design"
    assert r.assembly_tier == ASSEMBLY_TIER


@pytest.mark.asyncio
async def test_compose_trace_includes_fragment_and_skill_ids() -> None:
    writer = _RecordingWriter()
    frag_a = fake_fragment("f1", "execution", skill="sk-a")
    frag_b = fake_fragment("f2", "execution", skill="sk-b")
    domain = RetrievalResult(candidates=[frag_a, frag_b], eligible_count=2, retrieval_ms=10)
    orch = _FakeOrchestrator(domain, _empty_system(), writer)

    await orch.compose(_req())

    r = writer.records[0]
    assert r.domain_fragment_ids == ["f1", "f2"]
    assert r.source_skill_ids == ["sk-a", "sk-b"]


@pytest.mark.asyncio
async def test_compose_trace_includes_system_fragments() -> None:
    writer = _RecordingWriter()
    domain_frag = fake_fragment("d1", "execution", skill="sk-a")
    sys_frag = fake_fragment("s1", "execution", skill="sys-sk", skill_class="system")
    domain = RetrievalResult(candidates=[domain_frag], eligible_count=1, retrieval_ms=5)
    system = SystemRetrievalResult(
        candidates=[sys_frag], applied_skill_ids=["sys-sk"], retrieval_ms=1
    )
    orch = _FakeOrchestrator(domain, system, writer)

    await orch.compose(_req())

    r = writer.records[0]
    assert r.system_fragment_ids == ["s1"]


@pytest.mark.asyncio
async def test_compose_trace_captures_latency() -> None:
    writer = _RecordingWriter()
    frag = fake_fragment("f1", "execution", skill="sk-a")
    domain = RetrievalResult(candidates=[frag], eligible_count=1, retrieval_ms=15)
    orch = _FakeOrchestrator(domain, _empty_system(), writer)

    await orch.compose(_req())

    r = writer.records[0]
    assert r.latency_retrieval_ms is not None and r.latency_retrieval_ms >= 0
    assert r.latency_assembly_ms == 0
    assert r.latency_total_ms is not None and r.latency_total_ms >= 0


@pytest.mark.asyncio
async def test_compose_empty_writes_telemetry_record() -> None:
    writer = _RecordingWriter()
    domain = RetrievalResult(candidates=[], eligible_count=0, retrieval_ms=8)
    orch = _FakeOrchestrator(domain, _empty_system(), writer)

    await orch.compose(_req())

    assert len(writer.records) == 1
    r = writer.records[0]
    assert r.result_type == "compose_empty"
    assert r.task_prompt == "write a handler"
    assert r.domain_fragment_ids == []
    assert r.source_skill_ids == []


@pytest.mark.asyncio
async def test_compose_uses_bm25_fallback_candidates() -> None:
    writer = _RecordingWriter()
    frag = fake_fragment("f1", "execution", skill="sk-a")
    domain = EmbeddingErrorResult(
        error=EmbeddingError(EmbeddingErrorCode.UNAVAILABLE, "embed down"),
        bm25_only=True,
        candidates=[frag],
        eligible_count=1,
        retrieval_ms=6,
        scores_by_id={frag.fragment_id: 1.0},
    )
    orch = _FakeOrchestrator(domain, _empty_system(), writer)

    result = await orch.compose(_req())

    assert result.result_type == "composed"
    assert result.domain_fragments == ["f1"]
    record = writer.records[0]
    assert record.result_type == "compose"
    assert record.error_payload == EmbeddingErrorCode.UNAVAILABLE.value


@pytest.mark.asyncio
async def test_compose_empty_with_embedding_error_sets_error_payload() -> None:
    """Regression test: embedding fails AND BM25 returns no hits.

    The result should be EmptyResult with error_payload set to the embedding
    error code — distinguishable from a normal empty result where
    error_payload is None.
    """
    writer = _RecordingWriter()
    domain = EmbeddingErrorResult(
        error=EmbeddingError(EmbeddingErrorCode.UNAVAILABLE, "embed down"),
        bm25_only=True,
        candidates=[],
        eligible_count=0,
        retrieval_ms=6,
        scores_by_id={},
    )
    orch = _FakeOrchestrator(domain, _empty_system(), writer)

    result = await orch.compose(_req())

    assert result.result_type == "empty"
    assert len(writer.records) == 1
    r = writer.records[0]
    assert r.result_type == "compose_empty"
    assert r.error_payload == EmbeddingErrorCode.UNAVAILABLE.value
    assert r.domain_fragment_ids == []
    assert r.source_skill_ids == []


@pytest.mark.asyncio
async def test_compose_marks_dense_leg_degraded_on_embedding_error() -> None:
    """An embedding failure that falls back to BM25 is a degraded dense leg —
    surfaced on the response and the trace, not silent."""
    writer = _RecordingWriter()
    frag = fake_fragment("f1", "execution", skill="sk-a")
    domain = EmbeddingErrorResult(
        error=EmbeddingError(EmbeddingErrorCode.UNAVAILABLE, "embed down"),
        bm25_only=True,
        candidates=[frag],
        eligible_count=1,
        retrieval_ms=6,
        scores_by_id={frag.fragment_id: 1.0},
    )
    orch = _FakeOrchestrator(domain, _empty_system(), writer)

    result = await orch.compose(_req())

    assert result.dense_leg_degraded is True
    assert writer.records[0].dense_leg_degraded is True


@pytest.mark.asyncio
async def test_compose_marks_dense_leg_degraded_on_empty_query() -> None:
    """A RetrievalResult flagged degraded (empty bounded query -> dense skipped)
    propagates to the response and the trace."""
    writer = _RecordingWriter()
    frag = fake_fragment("f1", "execution", skill="sk-a")
    domain = RetrievalResult(
        candidates=[frag], eligible_count=1, retrieval_ms=4, dense_leg_degraded=True
    )
    orch = _FakeOrchestrator(domain, _empty_system(), writer)

    result = await orch.compose(_req())

    assert result.dense_leg_degraded is True
    assert writer.records[0].dense_leg_degraded is True


@pytest.mark.asyncio
async def test_compose_not_degraded_on_normal_retrieval() -> None:
    """A healthy dense leg leaves dense_leg_degraded False on response and trace."""
    writer = _RecordingWriter()
    frag = fake_fragment("f1", "execution", skill="sk-a")
    domain = RetrievalResult(candidates=[frag], eligible_count=1, retrieval_ms=10)
    orch = _FakeOrchestrator(domain, _empty_system(), writer)

    result = await orch.compose(_req())

    assert result.dense_leg_degraded is False
    assert writer.records[0].dense_leg_degraded is False
