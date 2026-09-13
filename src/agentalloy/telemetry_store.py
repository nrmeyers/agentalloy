"""Telemetry store: DuckDB-backed trace storage.

Python owns the single RW handle; Rust opens RO only (AC-7 boundary).
"""

from pathlib import Path
from typing import Any

import duckdb

from agentalloy.state_store import _LockedConn


class TelemetryStore:
    """DuckDB telemetry store for interpreter traces."""

    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        # Same threadpool-concurrency rules as StateStore: execute+fetch
        # must be atomic per call.
        self.conn = _LockedConn(duckdb.connect(str(self.db_path)))
        self._init_schema()

    def _init_schema(self) -> None:
        """Initialize tables."""
        self.conn.execute("""
            CREATE SEQUENCE IF NOT EXISTS traces_id_seq START 1;
            CREATE TABLE IF NOT EXISTS traces (
                id BIGINT DEFAULT nextval('traces_id_seq'),
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                session_id TEXT,
                step INTEGER,
                tool_name TEXT,
                tool_args TEXT,
                tool_result TEXT,
                stop_reason TEXT,
                phase TEXT
            )
        """)

    def record_trace(
        self,
        session_id: str,
        step: int,
        tool_name: str | None,
        tool_args: str,
        tool_result: str,
        stop_reason: str | None = None,
        phase: str | None = None,
    ) -> int:
        """Record a trace entry. Returns trace ID."""
        result = self.conn.execute(
            """
            INSERT INTO traces
                (session_id, step, tool_name, tool_args, tool_result, stop_reason, phase)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            [session_id, step, tool_name, tool_args, tool_result, stop_reason, phase],
        ).fetchone()
        return result[0] if result else 0

    def get_traces(self, k: int = 5, phase: str | None = None) -> list[dict[str, Any]]:
        """Get recent traces."""
        if phase:
            result = self.conn.execute(
                """
                SELECT id, timestamp, session_id, step, tool_name, tool_args, stop_reason, phase
                FROM traces WHERE phase = ?
                ORDER BY timestamp DESC LIMIT ?
                """,
                [phase, k],
            ).fetchall()
        else:
            result = self.conn.execute(
                """
                SELECT id, timestamp, session_id, step, tool_name, tool_args, stop_reason, phase
                FROM traces
                ORDER BY timestamp DESC LIMIT ?
                """,
                [k],
            ).fetchall()

        return [
            {
                "id": row[0],
                "timestamp": row[1].isoformat() if row[1] else None,
                "session_id": row[2],
                "step": row[3],
                "tool_name": row[4],
                "tool_args": row[5],
                "stop_reason": row[6],
                "phase": row[7],
            }
            for row in result
        ]

    def close(self) -> None:
        """Close the connection."""
        self.conn.close()

    # --- Analytics ---

    def record_run(
        self,
        session_id: str,
        tool_calls: list[dict[str, Any]],
        stop_reason: str,
        phase: str | None = None,
    ) -> None:
        """Record one interpreter run: a row per tool call plus a summary
        row (tool_name NULL) carrying the run's stop_reason. Results are
        truncated — telemetry is for analytics, not replay."""
        for i, call in enumerate(tool_calls):
            self.record_trace(
                session_id,
                i,
                str(call.get("tool", "")),
                str(call.get("args", ""))[:500],
                str(call.get("result", ""))[:500],
                stop_reason=None,
                phase=phase,
            )
        self.record_trace(
            session_id, len(tool_calls), None, "", "", stop_reason=stop_reason, phase=phase
        )

    def tool_usage_summary(self) -> dict[str, Any]:
        """Summary of tool usage across all traces."""
        rows = self.conn.execute("""
            SELECT tool_name, COUNT(*) as count
            FROM traces
            WHERE tool_name IS NOT NULL
            GROUP BY tool_name
            ORDER BY count DESC
        """).fetchall()
        return {
            "tools": [{"name": r[0], "count": r[1]} for r in rows],
            "total_calls": sum(r[1] for r in rows),
        }

    def phase_activity(self) -> dict[str, Any]:
        """Activity breakdown by phase."""
        rows = self.conn.execute("""
            SELECT phase, COUNT(*) as count, COUNT(DISTINCT session_id) as sessions
            FROM traces
            WHERE phase IS NOT NULL
            GROUP BY phase
            ORDER BY count DESC
        """).fetchall()
        return {"phases": [{"phase": r[0], "traces": r[1], "sessions": r[2]} for r in rows]}

    def stop_reason_distribution(self) -> dict[str, Any]:
        """Distribution of stop reasons."""
        rows = self.conn.execute("""
            SELECT stop_reason, COUNT(*) as count
            FROM traces
            WHERE stop_reason IS NOT NULL
            GROUP BY stop_reason
            ORDER BY count DESC
        """).fetchall()
        return {"reasons": [{"reason": r[0], "count": r[1]} for r in rows]}

    def session_summary(self, session_id: str) -> dict[str, Any]:
        """Detailed summary for a specific session."""
        trace_count = self.conn.execute(
            "SELECT COUNT(*) FROM traces WHERE session_id = ?", [session_id]
        ).fetchone()
        tools = self.conn.execute(
            """
            SELECT tool_name, COUNT(*) as count
            FROM traces WHERE session_id = ?
            GROUP BY tool_name ORDER BY count DESC
            """,
            [session_id],
        ).fetchall()
        phases = self.conn.execute(
            """
            SELECT DISTINCT phase FROM traces
            WHERE session_id = ? AND phase IS NOT NULL
            ORDER BY phase
            """,
            [session_id],
        ).fetchall()

        return {
            "session_id": session_id,
            "total_traces": trace_count[0] if trace_count else 0,
            "tools_used": [{"name": r[0], "count": r[1]} for r in tools],
            "phases_visited": [r[0] for r in phases],
        }

    def error_rate(self) -> dict[str, Any]:
        """Calculate error rate (tool_failed + validation_failed / total)."""
        total = self.conn.execute(
            "SELECT COUNT(*) FROM traces WHERE stop_reason IS NOT NULL"
        ).fetchone()
        errors = self.conn.execute(
            """
            SELECT COUNT(*) FROM traces
            WHERE stop_reason IN ('tool_failed', 'validation_failed')
            """
        ).fetchone()

        total_count = total[0] if total else 0
        error_count = errors[0] if errors else 0
        rate = error_count / total_count if total_count > 0 else 0.0

        return {
            "total_sessions": total_count,
            "error_sessions": error_count,
            "error_rate": round(rate, 4),
        }
