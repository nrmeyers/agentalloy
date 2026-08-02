"""Phase-event telemetry writer — lightweight per-phase event tracing.

Stores high-level lifecycle events (phase_start, phase_complete, phase_error,
llm_sent, llm_received, llm_error) in a dedicated ``phase_events`` table so
external dashboards and the proxy-signal route can answer "what happened when"
without parsing the heavier composition_traces table.

Writes are soft-fail: any exception is logged at DEBUG and never propagates,
so a broken telemetry layer cannot crash the calling code path.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PhaseEvent:
    """A single phase-level telemetry event.

    Immutable so it can be safely cached, replayed, or shipped to a
    background worker without mutation race conditions.
    """

    trace_id: str
    request_ts: int
    phase: str
    event_type: str
    model: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    latency_ms: int | None = None
    success: bool | None = None
    error_message: str | None = None
    workflow_skill_id: str | None = None
    system_prompt_sha: str | None = None
    direction: str | None = None
    repo: str | None = None


class TelemetryStore(Protocol):
    """Minimal protocol for the telemetry store — execute raw SQL."""

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None: ...


_INSERT_SQL = """\
INSERT INTO phase_events (
    trace_id, correlation_id, request_ts, phase, event_type,
    model, tokens_in, tokens_out, latency_ms, success,
    error_message, workflow_skill_id, system_prompt_sha, direction, repo
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""

_CREATE_DDL = """\
CREATE TABLE IF NOT EXISTS phase_events (
    trace_id VARCHAR,
    correlation_id VARCHAR,
    request_ts BIGINT NOT NULL,
    phase VARCHAR NOT NULL,
    event_type VARCHAR NOT NULL,
    model VARCHAR,
    tokens_in INTEGER,
    tokens_out INTEGER,
    latency_ms INTEGER,
    success BOOLEAN,
    error_message VARCHAR,
    workflow_skill_id VARCHAR,
    system_prompt_sha VARCHAR,
    direction VARCHAR,
    repo VARCHAR
);
CREATE INDEX IF NOT EXISTS idx_phase_events_ts ON phase_events(request_ts);
CREATE INDEX IF NOT EXISTS idx_phase_events_phase ON phase_events(phase);
CREATE INDEX IF NOT EXISTS idx_phase_events_type ON phase_events(event_type);
CREATE INDEX IF NOT EXISTS idx_phase_events_trace ON phase_events(trace_id);
"""

_ADD_REPO_COLUMN_SQL = "ALTER TABLE phase_events ADD COLUMN IF NOT EXISTS repo VARCHAR"
_CREATE_REPO_INDEX_SQL = "CREATE INDEX IF NOT EXISTS idx_phase_events_repo ON phase_events(repo)"


class PhaseTelemetryWriter:
    """Writes phase-level events into a DuckDB-backed telemetry store.

    Every public method is idempotent, soft-failing on any error so that
    telemetry problems never propagate to the caller.
    """

    def __init__(self, telemetry_store: TelemetryStore) -> None:
        self._store = telemetry_store
        self._init_done = False

    # -- public API ---------------------------------------------------------

    def phase_start(self, trace_id: str, phase: str, **kwargs: Any) -> None:
        self._write(trace_id, phase, event_type="phase_start", **kwargs)

    def phase_complete(self, trace_id: str, phase: str, **kwargs: Any) -> None:
        self._write(trace_id, phase, event_type="phase_complete", **kwargs)

    def phase_error(self, trace_id: str, phase: str, **kwargs: Any) -> None:
        self._write(trace_id, phase, event_type="phase_error", **kwargs)

    def llm_sent(self, trace_id: str, phase: str, **kwargs: Any) -> None:
        self._write(trace_id, phase, event_type="llm_sent", **kwargs)

    def llm_received(self, trace_id: str, phase: str, **kwargs: Any) -> None:
        self._write(trace_id, phase, event_type="llm_received", **kwargs)

    def llm_error(self, trace_id: str, phase: str, **kwargs: Any) -> None:
        self._write(trace_id, phase, event_type="llm_error", **kwargs)

    # -- internal -----------------------------------------------------------

    def _write(
        self,
        trace_id: str,
        phase: str,
        *,
        event_type: str,
        model: str | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        latency_ms: int | None = None,
        success: bool | None = None,
        error_message: str | None = None,
        workflow_skill_id: str | None = None,
        system_prompt_sha: str | None = None,
        direction: str | None = None,
        correlation_id: str | None = None,
        repo: str | None = None,
    ) -> None:
        try:
            self._ensure_schema()
            event = PhaseEvent(
                trace_id=trace_id,
                request_ts=int(time.time() * 1000),
                phase=phase,
                event_type=event_type,
                model=model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                latency_ms=latency_ms,
                success=success,
                error_message=error_message,
                workflow_skill_id=workflow_skill_id,
                system_prompt_sha=system_prompt_sha,
                direction=direction,
                repo=repo,
            )
            params: tuple[Any, ...] = (
                event.trace_id,
                correlation_id,
                event.request_ts,
                event.phase,
                event.event_type,
                event.model,
                event.tokens_in,
                event.tokens_out,
                event.latency_ms,
                event.success,
                event.error_message,
                event.workflow_skill_id,
                event.system_prompt_sha,
                event.direction,
                event.repo,
            )
            self._store.execute(_INSERT_SQL, params)
        except Exception:  # noqa: BLE001 — soft-fail by design
            logger.debug("phase_event write failed", exc_info=True)

    def _ensure_schema(self) -> None:
        if not self._init_done:
            try:
                self._store.execute(_CREATE_DDL)
            except Exception:  # noqa: BLE001
                logger.debug("phase_events schema creation failed", exc_info=True)
            try:
                # Self-healing migration: a DB built from a build between
                # 9eb7eca and 74ac10b already has a 14-column phase_events
                # table (CREATE TABLE IF NOT EXISTS was a no-op on it, even
                # though every INSERT from that era failed on arity and was
                # swallowed). ADD COLUMN IF NOT EXISTS is idempotent so this
                # is safe to run on every schema-init, fresh DB or old.
                self._store.execute(_ADD_REPO_COLUMN_SQL)
            except Exception:  # noqa: BLE001
                logger.debug("phase_events repo-column migration failed", exc_info=True)
            try:
                # Index creation must follow the column migration — on an old
                # DB the column (and thus the index target) doesn't exist
                # until the ALTER TABLE above runs.
                self._store.execute(_CREATE_REPO_INDEX_SQL)
            except Exception:  # noqa: BLE001
                logger.debug("phase_events repo-index creation failed", exc_info=True)
            finally:
                self._init_done = True
