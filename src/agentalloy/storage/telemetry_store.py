"""DuckDB-backed telemetry store (``telemetry.duck``) — composition traces.

Split into its own file (service-owned, append-only) so runtime trace writes
never contend with the reembed writer that holds ``agentalloy.duck`` (decision
D4). The analytics SQL (``aggregate_savings``, ``query_traces``) is preserved
verbatim — it is a wire contract with external dashboards / hook scripts
(decision D15).

Concurrency (decision D14): the old ``asyncio.to_thread`` offload is gone.
DuckDB connections are not thread-safe, so each thread gets its own cursor
(``base.cursor()``) over a single shared in-process database — the supported
DuckDB multithreading model. Telemetry reads are indexed + bounded, so they run
inline without blocking the event loop.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import duckdb

from agentalloy.storage.protocols import CompositionTrace

logger = logging.getLogger(__name__)


# One canonical CREATE — every column folded in (no per-open ALTER churn).
# Column order/types/defaults match the v5.3 composition_traces so external
# consumers see an identical shape. ``prompt_loads`` does not exist on the
# current tree and is intentionally not recreated.
_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS composition_traces (
    trace_id VARCHAR PRIMARY KEY,
    correlation_id VARCHAR,
    request_ts BIGINT NOT NULL,
    phase VARCHAR NOT NULL,
    category VARCHAR,
    task_prompt VARCHAR NOT NULL,
    selected_fragment_ids VARCHAR[],
    source_skill_ids VARCHAR[],
    system_skill_ids VARCHAR[],
    assembly_tier VARCHAR,
    assembly_model VARCHAR,
    retrieval_latency_ms INTEGER,
    assembly_latency_ms INTEGER,
    total_latency_ms INTEGER,
    status VARCHAR NOT NULL,
    error_code VARCHAR,
    response_size_chars INTEGER,
    prompt_version VARCHAR,
    workflow_skill_ids VARCHAR[],
    event_type VARCHAR NOT NULL DEFAULT 'compose',
    pre_filter_matched VARCHAR,
    gates_met VARCHAR[],
    gates_unmet VARCHAR[],
    qwen_calls INTEGER NOT NULL DEFAULT 0,
    contract_path VARCHAR,
    contract_tags VARCHAR[],
    bm25_source VARCHAR NOT NULL DEFAULT 'rule-extracted',
    reranked BOOLEAN NOT NULL DEFAULT FALSE,
    tokens_returned INTEGER NOT NULL DEFAULT 0,
    tokens_flat_equivalent INTEGER NOT NULL DEFAULT 0,
    lm_assist_outcome VARCHAR NOT NULL DEFAULT 'disabled',
    lm_assist_model VARCHAR,
    dense_leg_degraded BOOLEAN NOT NULL DEFAULT FALSE,
    phase_gate_embed_failed BOOLEAN NOT NULL DEFAULT FALSE,
    repo VARCHAR,
    session_key VARCHAR,
    session_source VARCHAR,
    lm_assist_kept_ids VARCHAR[],
    lm_assist_dropped_ids VARCHAR[],
    lm_assist_scores VARCHAR
);
CREATE INDEX IF NOT EXISTS idx_traces_ts ON composition_traces(request_ts);
CREATE INDEX IF NOT EXISTS idx_traces_phase ON composition_traces(phase);
CREATE INDEX IF NOT EXISTS idx_traces_status ON composition_traces(status);
CREATE INDEX IF NOT EXISTS idx_traces_repo ON composition_traces(repo);
CREATE INDEX IF NOT EXISTS idx_traces_session ON composition_traces(session_key);
"""


def _trace_where(
    *, phase: str | None, status: str | None, since: int | None, until: int | None
) -> tuple[str, list[object]]:
    clauses: list[str] = []
    params: list[object] = []
    if phase is not None:
        clauses.append("phase = ?")
        params.append(phase)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if since is not None:
        clauses.append("request_ts >= ?")
        params.append(since)
    if until is not None:
        clauses.append("request_ts <= ?")
        params.append(until)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def _repo_clause(repo: str | None) -> tuple[str, list[object]]:
    if repo is None:
        return "", []
    return "(repo = ? OR repo LIKE ?)", [repo, repo.rstrip("/") + "/%"]


class DuckDBTelemetryStore:
    """Composition-trace store with per-thread DuckDB cursors."""

    def __init__(self, db_path: str, *, read_only: bool = False) -> None:
        self._db_path = db_path
        self._read_only = read_only
        if not read_only:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._base = duckdb.connect(db_path, read_only=read_only)
        if not read_only:
            self._base.execute(_SCHEMA_DDL)
        self._local = threading.local()

    def _c(self) -> duckdb.DuckDBPyConnection:
        """Return this thread's cursor over the shared database."""
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._base.cursor()
            self._local.conn = c
        return c

    def close(self) -> None:
        try:
            self._base.close()
        except Exception:  # pragma: no cover - defensive
            logger.debug("failed to close telemetry connection", exc_info=True)

    # -- writes --------------------------------------------------------------

    def record_composition_trace(self, trace: CompositionTrace) -> None:
        """Insert a composition trace. Callers wrap in try/except so a telemetry
        failure never propagates to the /compose or proxy caller."""
        self._c().execute(
            """
            INSERT INTO composition_traces (
                trace_id, correlation_id, request_ts, phase, category,
                task_prompt, selected_fragment_ids, source_skill_ids,
                system_skill_ids, assembly_tier, assembly_model,
                retrieval_latency_ms, assembly_latency_ms, total_latency_ms,
                status, error_code, response_size_chars,
                prompt_version, workflow_skill_ids,
                event_type, pre_filter_matched, gates_met, gates_unmet, qwen_calls,
                contract_path, contract_tags, bm25_source, reranked,
                tokens_returned, tokens_flat_equivalent,
                lm_assist_outcome, lm_assist_model, dense_leg_degraded,
                phase_gate_embed_failed, repo, session_key, session_source,
                lm_assist_kept_ids, lm_assist_dropped_ids, lm_assist_scores
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                trace.trace_id,
                trace.correlation_id,
                trace.request_ts,
                trace.phase,
                trace.category,
                trace.task_prompt,
                trace.selected_fragment_ids,
                trace.source_skill_ids,
                trace.system_skill_ids,
                trace.assembly_tier,
                trace.assembly_model,
                trace.retrieval_latency_ms,
                trace.assembly_latency_ms,
                trace.total_latency_ms,
                trace.status,
                trace.error_code,
                trace.response_size_chars,
                trace.prompt_version,
                trace.workflow_skill_ids,
                trace.event_type,
                trace.pre_filter_matched,
                trace.gates_met,
                trace.gates_unmet,
                trace.qwen_calls,
                trace.contract_path,
                trace.contract_tags,
                trace.bm25_source,
                trace.reranked,
                trace.tokens_returned,
                trace.tokens_flat_equivalent,
                trace.lm_assist_outcome,
                trace.lm_assist_model,
                trace.dense_leg_degraded,
                trace.phase_gate_embed_failed,
                trace.repo,
                trace.session_key,
                trace.session_source,
                trace.lm_assist_kept_ids,
                trace.lm_assist_dropped_ids,
                trace.lm_assist_scores,
            ],
        )

    def clear_telemetry(self) -> dict[str, int]:
        """Delete all composition_traces rows. Returns counts of deleted rows."""
        traces = self.count_traces()
        self._c().execute("DELETE FROM composition_traces")
        return {"traces_deleted": traces}

    # -- reads ---------------------------------------------------------------

    def count_traces(self) -> int:
        row = self._c().execute("SELECT COUNT(*) FROM composition_traces").fetchone()
        return int(row[0]) if row else 0

    def count_traces_filtered(
        self,
        *,
        phase: str | None = None,
        status: str | None = None,
        since: int | None = None,
        until: int | None = None,
    ) -> int:
        where, params = _trace_where(phase=phase, status=status, since=since, until=until)
        row = (
            self._c().execute(f"SELECT COUNT(*) FROM composition_traces {where}", params).fetchone()
        )
        return int(row[0]) if row else 0

    def query_traces(
        self,
        *,
        phase: str | None = None,
        status: str | None = None,
        since: int | None = None,
        until: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[CompositionTrace]:
        where, params = _trace_where(phase=phase, status=status, since=since, until=until)
        sql = f"""
            SELECT trace_id, correlation_id, request_ts, phase, category,
                   task_prompt, selected_fragment_ids, source_skill_ids,
                   system_skill_ids, assembly_tier, assembly_model,
                   retrieval_latency_ms, assembly_latency_ms, total_latency_ms,
                   status, error_code, response_size_chars, prompt_version,
                   workflow_skill_ids, event_type, pre_filter_matched,
                   gates_met, gates_unmet, qwen_calls,
                   contract_path, contract_tags, bm25_source, reranked,
                   tokens_returned, tokens_flat_equivalent,
                   lm_assist_outcome, lm_assist_model, dense_leg_degraded,
                   phase_gate_embed_failed, repo, session_key, session_source,
                   lm_assist_kept_ids, lm_assist_dropped_ids, lm_assist_scores
            FROM composition_traces
            {where}
            ORDER BY request_ts DESC
            LIMIT ? OFFSET ?
        """
        rows = self._c().execute(sql, params + [limit, offset]).fetchall()
        return [
            CompositionTrace(
                trace_id=str(r[0]),
                correlation_id=r[1],
                request_ts=int(r[2]),
                phase=str(r[3]),
                category=r[4],
                task_prompt=str(r[5]),
                selected_fragment_ids=list(r[6] or []),
                source_skill_ids=list(r[7] or []),
                system_skill_ids=list(r[8] or []),
                assembly_tier=r[9],
                assembly_model=r[10],
                retrieval_latency_ms=r[11],
                assembly_latency_ms=r[12],
                total_latency_ms=r[13],
                status=str(r[14]),
                error_code=r[15],
                response_size_chars=r[16],
                prompt_version=r[17],
                workflow_skill_ids=list(r[18] or []),
                event_type=r[19] or "compose",
                pre_filter_matched=r[20],
                gates_met=list(r[21] or []),
                gates_unmet=list(r[22] or []),
                qwen_calls=int(r[23]) if r[23] else 0,
                contract_path=r[24],
                contract_tags=list(r[25] or []),
                bm25_source=r[26] or "rule-extracted",
                reranked=bool(r[27]),
                tokens_returned=int(r[28]) if r[28] is not None else 0,
                tokens_flat_equivalent=int(r[29]) if r[29] is not None else 0,
                lm_assist_outcome=str(r[30]) if r[30] is not None else "disabled",
                lm_assist_model=r[31],
                dense_leg_degraded=bool(r[32]),
                phase_gate_embed_failed=bool(r[33]),
                repo=r[34],
                session_key=r[35],
                session_source=r[36],
                lm_assist_kept_ids=list(r[37] or []),
                lm_assist_dropped_ids=list(r[38] or []),
                lm_assist_scores=r[39],
            )
            for r in rows
        ]

    def aggregate_savings(self, repo: str | None = None) -> dict[str, object]:
        """Aggregate token-savings telemetry across proxy compose traces.

        Counts ``status='proxy_composed'`` rows (one per composed request).
        Preserved verbatim from v5.3 — the output shape is a dashboard contract.
        """
        repo_clause, repo_params = _repo_clause(repo)
        repo_and = f" AND {repo_clause}" if repo_clause else ""

        overall = (
            self._c()
            .execute(
                f"""
            SELECT
                COUNT(*) AS total_composes,
                COALESCE(SUM(tokens_returned), 0) AS sum_returned,
                COALESCE(SUM(tokens_flat_equivalent), 0) AS sum_flat
            FROM composition_traces
            WHERE status = 'proxy_composed'{repo_and}
            """,
                repo_params,
            )
            .fetchone()
        )
        total_composes = int(overall[0]) if overall else 0
        sum_returned = int(overall[1]) if overall else 0
        sum_flat = int(overall[2]) if overall else 0
        tokens_saved = max(0, sum_flat - sum_returned)
        savings_pct = round(tokens_saved / sum_flat * 100, 1) if sum_flat > 0 else 0.0

        phase_rows = (
            self._c()
            .execute(
                f"""
            SELECT
                phase,
                COUNT(*) AS composes,
                COALESCE(SUM(tokens_returned), 0) AS returned,
                COALESCE(SUM(tokens_flat_equivalent), 0) AS flat
            FROM composition_traces
            WHERE status = 'proxy_composed'{repo_and}
            GROUP BY phase
            ORDER BY composes DESC
            """,
                repo_params,
            )
            .fetchall()
        )
        per_phase: list[dict[str, object]] = []
        for row in phase_rows:
            ph_flat = int(row[3])
            ph_returned = int(row[2])
            ph_saved = max(0, ph_flat - ph_returned)
            ph_pct = round(ph_saved / ph_flat * 100, 1) if ph_flat > 0 else 0.0
            per_phase.append(
                {
                    "phase": str(row[0]),
                    "composes": int(row[1]),
                    "tokens_returned": ph_returned,
                    "tokens_flat_equivalent": ph_flat,
                    "tokens_saved": ph_saved,
                    "savings_pct": ph_pct,
                }
            )

        return {
            "total_composes": total_composes,
            "tokens_returned": sum_returned,
            "tokens_flat_equivalent": sum_flat,
            "tokens_saved": tokens_saved,
            "savings_pct": savings_pct,
            "per_phase": per_phase,
        }


def open_telemetry_store(db_path: str | Path, *, read_only: bool = False) -> DuckDBTelemetryStore:
    return DuckDBTelemetryStore(str(db_path), read_only=read_only)
