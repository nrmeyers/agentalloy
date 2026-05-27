"""Telemetry writer protocol, no-op stub, and DuckDB-backed writer.

Per v5.3, composition telemetry lives in DuckDB ``composition_traces``
(same ``skills.duck`` file as fragment_embeddings). Writes are
synchronous on the request path. Trace-write failures are caught and
logged but never propagate to the caller of /compose — the response
always succeeds regardless of telemetry state.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from agentalloy.storage.vector_store import (
    CompositionTrace as DuckCompositionTrace,
)
from agentalloy.storage.vector_store import (
    VectorStore,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TelemetryRecord:
    """Structured trace payload. Retrieval-only records leave assembly fields None."""

    composition_id: str
    timestamp: datetime
    phase: str | None
    task_prompt: str
    result_type: str
    requesting_agent: str | None = None
    retrieval_tier: int | None = None
    assembly_tier: int | None = None
    domain_fragment_ids: list[str] | None = None
    system_fragment_ids: list[str] | None = None
    source_skill_ids: list[str] | None = None
    output: str | None = None
    latency_retrieval_ms: int | None = None
    latency_assembly_ms: int | None = None
    latency_total_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    error_payload: str | None = None
    workflow_skill_ids: list[str] | None = None
    prompt_version: str | None = None


class TelemetryWriter(Protocol):
    def write(self, record: TelemetryRecord) -> None: ...


class NullTelemetryWriter:
    """No-op writer. Logs at DEBUG so traces surface in dev without DB dependency."""

    def write(self, record: TelemetryRecord) -> None:
        logger.debug(
            "telemetry(null) id=%s type=%s phase=%s domain=%d system=%d",
            record.composition_id,
            record.result_type,
            record.phase,
            len(record.domain_fragment_ids or []),
            len(record.system_fragment_ids or []),
        )


class DuckDBTelemetryWriter:
    """Best-effort DuckDB telemetry writer.

    Writes happen synchronously on the request path. Telemetry is
    best-effort — failures are caught, logged, and never propagate to
    the caller. The response always succeeds regardless of telemetry
    state. The broad ``except Exception`` on :meth:`write` is intentional
    by design.

    ``TelemetryRecord`` (legacy v1.0 shape) maps to
    ``CompositionTrace`` (v5.3 schema) via :meth:`_to_duck_trace`.
    """

    def __init__(self, vector_store: VectorStore) -> None:
        self._vs = vector_store

    def write(self, record: TelemetryRecord) -> None:
        try:
            self._vs.record_composition_trace(self._to_duck_trace(record))
        except Exception as exc:
            logger.error("telemetry write failed: %s", exc)

    def close(self) -> None:  # noqa: B027 — empty by design; the vector_store owns the connection
        """No-op. The ``VectorStore`` owns the DuckDB connection lifecycle."""

    @staticmethod
    def _to_duck_trace(record: TelemetryRecord) -> DuckCompositionTrace:
        # Map v1.0 TelemetryRecord → v5.3 composition_traces row.
        # ``selected_fragment_ids`` collapses domain + system fragment ids into
        # one list so the v5.3 schema's "what was selected" column is
        # round-trippable. ``system_skill_ids`` reuses the system_fragment_ids
        # field name from the legacy record (semantic shift documented in
        # v5.3 §2.4.2 — both list[str] of identifiers).
        request_ts = int(record.timestamp.timestamp())
        selected: list[str] = []
        if record.domain_fragment_ids:
            selected.extend(record.domain_fragment_ids)
        if record.system_fragment_ids:
            selected.extend(record.system_fragment_ids)
        return DuckCompositionTrace(
            trace_id=record.composition_id,
            request_ts=request_ts,
            phase=record.phase or "unspecified",
            task_prompt=record.task_prompt,
            status=record.result_type,
            correlation_id=None,
            category=None,
            selected_fragment_ids=selected,
            source_skill_ids=list(record.source_skill_ids or []),
            system_skill_ids=list(record.system_fragment_ids or []),
            assembly_tier=str(record.assembly_tier) if record.assembly_tier is not None else None,
            assembly_model=None,
            retrieval_latency_ms=record.latency_retrieval_ms,
            assembly_latency_ms=record.latency_assembly_ms,
            total_latency_ms=record.latency_total_ms,
            error_code=record.error_payload,
            response_size_chars=len(record.output) if record.output is not None else None,
            workflow_skill_ids=list(record.workflow_skill_ids or []),
            prompt_version=record.prompt_version,
        )


def _new_trace_id() -> str:
    """Helper for orchestrators that don't have a composition_id ready."""
    return str(uuid.uuid4())
