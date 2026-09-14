"""Cumulative token usage tracking for the steering proxy.

Tracks per-request and session-level token consumption, including
injected tokens from steering context. Exposes totals for harness
session summaries.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import duckdb


@dataclass
class UsageRecord:
    """A single request's token usage."""

    prompt_tokens: int
    completion_tokens: int
    injected_tokens: int
    timestamp: str


class UsageTracker:
    """Track cumulative token usage across proxy requests."""

    def __init__(self, db_path: str = "./usage.duck") -> None:
        self.conn = duckdb.connect(db_path)
        self._init_tables()

    def _init_tables(self) -> None:
        self.conn.execute("CREATE SEQUENCE IF NOT EXISTS usage_seq")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS usage (
                id BIGINT DEFAULT nextval('usage_seq'),
                prompt_tokens INTEGER NOT NULL,
                completion_tokens INTEGER NOT NULL,
                injected_tokens INTEGER NOT NULL DEFAULT 0,
                timestamp VARCHAR NOT NULL
            )
        """)

    def record(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        injected_tokens: int = 0,
    ) -> None:
        """Record a single request's usage."""
        self.conn.execute(
            "INSERT INTO usage (prompt_tokens, completion_tokens, injected_tokens, timestamp) "
            "VALUES (?, ?, ?, ?)",
            [
                prompt_tokens,
                completion_tokens,
                injected_tokens,
                datetime.now().isoformat(),
            ],
        )

    def get_totals(self) -> dict[str, int]:
        """Get session-level cumulative totals."""
        try:
            row = self.conn.execute(
                "SELECT COALESCE(SUM(prompt_tokens), 0), "
                "COALESCE(SUM(completion_tokens), 0), "
                "COALESCE(SUM(injected_tokens), 0), "
                "COUNT(*) "
                "FROM usage"
            ).fetchone()
            if row:
                return {
                    "prompt_tokens": row[0],
                    "completion_tokens": row[1],
                    "injected_tokens": row[2],
                    "total_requests": row[3],
                    "total_tokens": row[0] + row[1],
                }
        except duckdb.Error:
            pass
        return {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "injected_tokens": 0,
            "total_requests": 0,
            "total_tokens": 0,
        }

    def get_history(self, limit: int = 50) -> list[UsageRecord]:
        """Get recent usage history."""
        try:
            rows = self.conn.execute(
                "SELECT prompt_tokens, completion_tokens, injected_tokens, timestamp "
                "FROM usage ORDER BY id DESC LIMIT ?",
                [limit],
            ).fetchall()
            return [
                UsageRecord(
                    prompt_tokens=r[0],
                    completion_tokens=r[1],
                    injected_tokens=r[2],
                    timestamp=r[3],
                )
                for r in rows
            ]
        except duckdb.Error:
            return []

    def close(self) -> None:
        self.conn.close()
