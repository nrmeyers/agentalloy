"""Telemetry read endpoint — GET /telemetry/traces.

Exposes paginated, filterable access to the ``composition_traces`` table
written by ``DuckDBTelemetryWriter``. Read-only; traces are append-only.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from agentalloy.storage.protocols import CompositionTrace, TelemetryStore

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
    repo: str | None = None
    session_key: str | None = None
    session_source: str | None = None
    # Signal/retrieval story (persisted since v5.3; exposed for the web UI's
    # trace expander — "why did/didn't composition fire?").
    event_type: str = "compose"
    pre_filter_matched: str | None = None
    gates_met: list[str] = []
    gates_unmet: list[str] = []
    qwen_calls: int = 0
    contract_path: str | None = None
    contract_tags: list[str] = []
    bm25_source: str = "rule-extracted"
    reranked: bool = False
    tokens_returned: int = 0
    tokens_flat_equivalent: int = 0
    lm_assist_outcome: str = "disabled"
    lm_assist_model: str | None = None
    lm_assist_kept_ids: list[str] = []
    lm_assist_dropped_ids: list[str] = []
    lm_assist_scores: str | None = None
    dense_leg_degraded: bool = False
    phase_gate_embed_failed: bool = False

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
            repo=t.repo,
            session_key=t.session_key,
            session_source=t.session_source,
            event_type=t.event_type,
            pre_filter_matched=t.pre_filter_matched,
            gates_met=t.gates_met,
            gates_unmet=t.gates_unmet,
            qwen_calls=t.qwen_calls,
            contract_path=t.contract_path,
            contract_tags=t.contract_tags,
            bm25_source=t.bm25_source,
            reranked=t.reranked,
            tokens_returned=t.tokens_returned,
            tokens_flat_equivalent=t.tokens_flat_equivalent,
            lm_assist_outcome=t.lm_assist_outcome,
            lm_assist_model=t.lm_assist_model,
            lm_assist_kept_ids=t.lm_assist_kept_ids,
            lm_assist_dropped_ids=t.lm_assist_dropped_ids,
            lm_assist_scores=t.lm_assist_scores,
            dense_leg_degraded=t.dense_leg_degraded,
            phase_gate_embed_failed=t.phase_gate_embed_failed,
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
    """Token-savings aggregation — mirrors ``TelemetryStore.aggregate_savings()``."""

    total_composes: int
    tokens_returned: int
    tokens_flat_equivalent: int
    tokens_saved: int
    savings_pct: float
    per_phase: list[PhaseSavings]


class PhaseCoverage(BaseModel):
    phase: str
    composed: int
    passthrough: int


class RepoCoverage(BaseModel):
    repo: str | None
    composed: int
    passthrough: int


class CoverageResponse(BaseModel):
    """Coverage v2 — mirrors ``TelemetryStore.aggregate_coverage()``."""

    total: int
    composed: int
    passthrough: int
    compose_rate: float
    per_phase: list[PhaseCoverage]
    per_repo: list[RepoCoverage]


class TelemetryQuerier:
    def __init__(self, store: TelemetryStore) -> None:
        self._store = store

    async def savings(self, repo: str | None = None) -> SavingsResponse:
        # telemetry.duck reads use thread-local cursors; run off the loop anyway.
        data = await asyncio.to_thread(self._store.aggregate_savings, repo)
        return SavingsResponse.model_validate(data)

    async def coverage(self, repo: str | None = None) -> CoverageResponse:
        data = await asyncio.to_thread(self._store.aggregate_coverage, repo)
        return CoverageResponse.model_validate(data)

    async def query(
        self,
        *,
        phase: str | None,
        status: str | None,
        since: int | None,
        until: int | None,
        repo: str | None,
        limit: int,
        offset: int,
    ) -> TracesResponse:
        kwargs: dict[str, Any] = dict(
            phase=phase, status=status, since=since, until=until, repo=repo
        )
        # Off-loop reads via the telemetry store's thread-local cursors.
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
    repo: str | None = Query(
        default=None,
        description="Scope to a project root (matches it or any subdirectory).",
    ),
) -> TracesResponse:
    querier: TelemetryQuerier | None = getattr(request.app.state, "telemetry_querier", None)
    if querier is None:
        return _empty_response(limit, offset)
    return await querier.query(
        phase=phase,
        status=status,
        since=since,
        until=until,
        repo=repo,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/telemetry/savings",
    response_model=SavingsResponse,
    summary="Token-savings aggregation across compose traces",
)
async def get_savings(
    request: Request,
    repo: str | None = Query(
        default=None,
        description="Scope to a project root (matches it or any subdirectory); omit for all repos.",
    ),
) -> SavingsResponse:
    """Return token-savings totals + per-phase breakdown from the open store.

    The CLI (`agentalloy telemetry savings`) calls this when the service is up,
    rather than opening the DuckDB directly — the service holds the single
    read-write lock, so a direct open would conflict. ``repo`` scopes the
    aggregation to a single project root (the CLI's default), omitted for ``--all``.
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
    return await querier.savings(repo)


@router.get(
    "/telemetry/coverage",
    response_model=CoverageResponse,
    summary="Coverage v2 — composed vs passthrough rate per phase and repo",
)
async def get_coverage(
    request: Request,
    repo: str | None = Query(
        default=None,
        description="Scope to a project root (matches it or any subdirectory); omit for all repos.",
    ),
) -> CoverageResponse:
    """How often composition fires vs passes through, over proxy traces.

    Replaces the hook-era coverage roll-up removed in v3.11.0 — same question,
    answered from the consolidated ``composition_traces`` rows (``event_type``
    ``proxy_composed`` vs ``proxy_passthrough``).
    """
    querier: TelemetryQuerier | None = getattr(request.app.state, "telemetry_querier", None)
    if querier is None:
        return CoverageResponse(
            total=0, composed=0, passthrough=0, compose_rate=0.0, per_phase=[], per_repo=[]
        )
    return await querier.coverage(repo)
