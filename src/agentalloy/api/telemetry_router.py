"""Telemetry read endpoint — GET /telemetry/traces.

Exposes paginated, filterable access to the ``composition_traces`` table
written by ``DuckDBTelemetryWriter``. Read-only; traces are append-only.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from agentalloy.storage.vector_store import CompositionTrace, VectorStore

router = APIRouter()

_MAX_LIMIT = 200


class TraceRecord(BaseModel):
    trace_id: str
    correlation_id: str | None
    request_ts: int
    phase: str
    category: str | None
    task_prompt: str
    selected_fragment_ids: list[str]
    source_skill_ids: list[str]
    system_skill_ids: list[str]
    workflow_skill_ids: list[str]
    assembly_tier: str | None
    assembly_model: str | None
    retrieval_latency_ms: int | None
    assembly_latency_ms: int | None
    total_latency_ms: int | None
    status: str
    error_code: str | None
    response_size_chars: int | None
    prompt_version: str | None

    @classmethod
    def from_trace(cls, t: CompositionTrace) -> TraceRecord:
        return cls(
            trace_id=t.trace_id,
            correlation_id=t.correlation_id,
            request_ts=t.request_ts,
            phase=t.phase,
            category=t.category,
            task_prompt=t.task_prompt,
            selected_fragment_ids=t.selected_fragment_ids,
            source_skill_ids=t.source_skill_ids,
            system_skill_ids=t.system_skill_ids,
            workflow_skill_ids=t.workflow_skill_ids,
            assembly_tier=t.assembly_tier,
            assembly_model=t.assembly_model,
            retrieval_latency_ms=t.retrieval_latency_ms,
            assembly_latency_ms=t.assembly_latency_ms,
            total_latency_ms=t.total_latency_ms,
            status=t.status,
            error_code=t.error_code,
            response_size_chars=t.response_size_chars,
            prompt_version=t.prompt_version,
        )


class TracesResponse(BaseModel):
    total: int
    offset: int
    limit: int
    traces: list[TraceRecord]


class PhaseSavings(BaseModel):
    phase: str
    composes: int
    tokens_returned: int
    tokens_flat_equivalent: int
    tokens_saved: int
    savings_pct: float


class SavingsResponse(BaseModel):
    """Token-savings aggregation — mirrors ``VectorStore.aggregate_savings()``."""

    total_composes: int
    tokens_returned: int
    tokens_flat_equivalent: int
    tokens_saved: int
    savings_pct: float
    per_phase: list[PhaseSavings]


class TelemetryQuerier:
    def __init__(self, store: VectorStore) -> None:
        self._store = store

    async def savings(self) -> SavingsResponse:
        # DuckDB connection is not thread-safe; run the read off the loop.
        data = await asyncio.to_thread(self._store.aggregate_savings)
        return SavingsResponse.model_validate(data)

    async def query(
        self,
        *,
        phase: str | None,
        status: str | None,
        since: int | None,
        until: int | None,
        limit: int,
        offset: int,
    ) -> TracesResponse:
        kwargs: dict[str, Any] = dict(phase=phase, status=status, since=since, until=until)
        # Both reads share one DuckDB connection, which is not thread-safe
        # (see VectorStore docstring) — run them sequentially, not via gather.
        traces = await asyncio.to_thread(
            self._store.query_traces, **kwargs, limit=limit, offset=offset
        )
        total = await asyncio.to_thread(self._store.count_traces_filtered, **kwargs)
        return TracesResponse(
            total=total,
            offset=offset,
            limit=limit,
            traces=[TraceRecord.from_trace(t) for t in traces],
        )


def _empty_response(limit: int, offset: int) -> TracesResponse:
    return TracesResponse(total=0, offset=offset, limit=limit, traces=[])


@router.get(
    "/telemetry/traces",
    response_model=TracesResponse,
    summary="List composition traces with optional filtering and pagination",
)
async def list_traces(
    request: Request,
    limit: int = Query(default=50, ge=1, le=_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    phase: str | None = Query(default=None),
    status: str | None = Query(default=None),
    since: int | None = Query(default=None, description="Unix epoch ms lower bound"),
    until: int | None = Query(default=None, description="Unix epoch ms upper bound"),
) -> TracesResponse:
    querier: TelemetryQuerier | None = getattr(request.app.state, "telemetry_querier", None)
    if querier is None:
        return _empty_response(limit, offset)
    return await querier.query(
        phase=phase,
        status=status,
        since=since,
        until=until,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/telemetry/savings",
    response_model=SavingsResponse,
    summary="Token-savings aggregation across compose traces",
)
async def get_savings(request: Request) -> SavingsResponse:
    """Return token-savings totals + per-phase breakdown from the open store.

    The CLI (`agentalloy telemetry savings`) calls this when the service is up,
    rather than opening the DuckDB directly — the service holds the single
    read-write lock, so a direct open would conflict.
    """
    querier: TelemetryQuerier | None = getattr(request.app.state, "telemetry_querier", None)
    if querier is None:
        return SavingsResponse(
            total_composes=0,
            tokens_returned=0,
            tokens_flat_equivalent=0,
            tokens_saved=0,
            savings_pct=0.0,
            per_phase=[],
        )
    return await querier.savings()
