"""Local-agent telemetry — one row per /local-agent/ask (M1d).

A dedicated ``local_agent_traces`` table in the service-owned
``telemetry.duck`` (same DuckDB database as ``composition_traces`` and
``phase_events`` — a new table, not a new file, per the design). Mirrors the
``PhaseTelemetryWriter`` pattern: lazy schema DDL (once per process) and
soft-failing writes — telemetry must never fail the request.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Protocol

from agentalloy.local_agent.loop import LoopResult

logger = logging.getLogger(__name__)


class TelemetryStore(Protocol):
    """Minimal protocol for the telemetry store — execute raw SQL."""

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None: ...


_CREATE_DDL = """\
CREATE TABLE IF NOT EXISTS local_agent_traces (
    trace_id VARCHAR PRIMARY KEY,
    request_ts BIGINT NOT NULL,
    phase VARCHAR,
    repo VARCHAR,
    question VARCHAR NOT NULL,
    model_tag VARCHAR NOT NULL,
    stop_reason VARCHAR NOT NULL,
    degraded BOOLEAN NOT NULL,
    degrade_reason VARCHAR,
    steps_count INTEGER NOT NULL,
    actions VARCHAR[],
    stage_latency_ms VARCHAR,
    total_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_local_agent_traces_ts ON local_agent_traces(request_ts);
CREATE INDEX IF NOT EXISTS idx_local_agent_traces_repo ON local_agent_traces(repo);
"""

_INSERT_SQL = """\
INSERT INTO local_agent_traces (
    trace_id, request_ts, phase, repo, question, model_tag,
    stop_reason, degraded, degrade_reason, steps_count, actions,
    stage_latency_ms, total_ms
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
"""


class LocalAgentTraceWriter:
    """Writes one trace row per local-agent request.

    Every write is idempotent and soft-failing on any error, so a broken
    telemetry layer cannot fail the request path.
    """

    def __init__(self, telemetry_store: TelemetryStore) -> None:
        self._store = telemetry_store
        self._init_done = False

    def record(
        self,
        *,
        result: LoopResult,
        question: str,
        phase: str | None = None,
        repo: str | None = None,
    ) -> None:
        """Persist one finished (or degraded) request as a single row."""
        stage_totals: dict[str, int] = {}
        for step in result.steps:
            for stage, ms in step.stage_latencies_ms.items():
                stage_totals[stage] = stage_totals.get(stage, 0) + ms
        try:
            self._ensure_schema()
            self._store.execute(
                _INSERT_SQL,
                (
                    uuid.uuid4().hex,
                    int(time.time() * 1000),
                    phase,
                    repo,
                    question,
                    result.model_tag,
                    result.stop_reason,
                    result.degraded,
                    result.degrade_reason,
                    len(result.steps),
                    [s.action for s in result.steps],
                    json.dumps(stage_totals, sort_keys=True),
                    result.total_ms,
                ),
            )
        except Exception:  # noqa: BLE001 — soft-fail by design
            logger.debug("local_agent trace write failed", exc_info=True)

    def _ensure_schema(self) -> None:
        if not self._init_done:
            try:
                self._store.execute(_CREATE_DDL)
            except Exception:  # noqa: BLE001
                logger.debug("local_agent_traces schema creation failed", exc_info=True)
            finally:
                self._init_done = True
