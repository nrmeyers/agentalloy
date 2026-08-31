"""Tests for the phase-event telemetry writer (Task 01 of add-telemetry)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from agentalloy.telemetry.phase_writer import (
    PhaseEvent,
    PhaseTelemetryWriter,
)


class _MockStore:
    """Minimal mock of the TelemetryStore protocol for unit tests."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.calls.append((sql, params or ()))


def _now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# T1 — phase_start writes correct row
# ---------------------------------------------------------------------------


class TestPhaseStart:
    def test_writes_correct_row(self) -> None:
        store = _MockStore()
        writer = PhaseTelemetryWriter(store)
        trace_id = "trace-001"
        phase = "compose"

        writer.phase_start(trace_id, phase, model="gpt-4", tokens_in=100, tokens_out=50)

        assert (
            len(store.calls) == 7
        )  # DDL + ADD COLUMN + ADD_WORKFLOW_DELIVERED + ADD PREV_PHASE + ADD TRANSITIONED_BY + CREATE INDEX + INSERT
        sql, params = store.calls[-1]
        assert "INSERT INTO phase_events" in sql
        assert params[0] == trace_id  # trace_id
        assert params[3] == phase  # phase
        assert params[4] == "phase_start"  # event_type
        assert params[5] == "gpt-4"  # model
        assert params[6] == 100  # tokens_in
        assert params[7] == 50  # tokens_out
        assert params[10] is None  # success
        assert params[11] is None  # error_message
        assert params[15] is None  # workflow_delivered

    def test_success_flag_is_written(self) -> None:
        store = _MockStore()
        writer = PhaseTelemetryWriter(store)
        writer.phase_start("t1", "p1", success=True)
        sql, params = store.calls[-1]
        assert params[9] is True  # success column

    def test_correlation_id_is_passed_through(self) -> None:
        store = _MockStore()
        writer = PhaseTelemetryWriter(store)
        writer.phase_start("t1", "p1", correlation_id="corr-1")
        sql, params = store.calls[-1]
        assert params[1] == "corr-1"  # correlation_id column


# ---------------------------------------------------------------------------
# T2 — phase_complete writes correct row
# ---------------------------------------------------------------------------


class TestPhaseComplete:
    def test_writes_correct_row(self) -> None:
        store = _MockStore()
        writer = PhaseTelemetryWriter(store)
        writer.phase_complete("t1", "compose", latency_ms=250, success=True)
        sql, params = store.calls[-1]
        assert params[4] == "phase_complete"
        assert params[8] == 250  # latency_ms
        assert params[9] is True  # success

    def test_error_message_is_written(self) -> None:
        store = _MockStore()
        writer = PhaseTelemetryWriter(store)
        writer.phase_complete("t1", "assemble", error_message="timeout")
        sql, params = store.calls[-1]
        assert params[10] == "timeout"


# ---------------------------------------------------------------------------
# T3 — llm_sent writes correct row
# ---------------------------------------------------------------------------


class TestLlmSent:
    def test_writes_correct_row(self) -> None:
        store = _MockStore()
        writer = PhaseTelemetryWriter(store)
        writer.llm_sent("t1", "llm", model="claude-sonnet-4-5-20250514", tokens_out=300)
        sql, params = store.calls[-1]
        assert params[4] == "llm_sent"
        assert params[5] == "claude-sonnet-4-5-20250514"
        assert params[7] == 300  # tokens_out


# ---------------------------------------------------------------------------
# T4 — llm_received writes correct row
# ---------------------------------------------------------------------------


class TestLlmReceived:
    def test_writes_correct_row(self) -> None:
        store = _MockStore()
        writer = PhaseTelemetryWriter(store)
        writer.llm_received("t1", "llm", tokens_in=300, latency_ms=5000)
        sql, params = store.calls[-1]
        assert params[4] == "llm_received"
        assert params[6] == 300  # tokens_in
        assert params[8] == 5000  # latency_ms


# ---------------------------------------------------------------------------
# T5 — llm_error writes correct row
# ---------------------------------------------------------------------------


class TestLlmError:
    def test_writes_correct_row(self) -> None:
        store = _MockStore()
        writer = PhaseTelemetryWriter(store)
        writer.llm_error("t1", "llm", error_message="rate limit", success=False)
        sql, params = store.calls[-1]
        assert params[4] == "llm_error"
        assert params[10] == "rate limit"
        assert params[9] is False


# ---------------------------------------------------------------------------
# T5b — phase_transition writes correct row
# ---------------------------------------------------------------------------


class TestPhaseTransition:
    def test_writes_correct_row(self) -> None:
        store = _MockStore()
        writer = PhaseTelemetryWriter(store)
        writer.phase_transition(
            "t1", "add-skill", prev_phase="intake", transitioned_by="f2ctl", repo="/home/nate/proj"
        )
        sql, params = store.calls[-1]
        assert "INSERT INTO phase_events" in sql
        assert params[0] == "t1"  # trace_id
        assert params[3] == "add-skill"  # phase
        assert params[4] == "phase_transition"  # event_type
        assert params[16] == "intake"  # prev_phase
        assert params[17] == "f2ctl"  # transitioned_by
        # No LLM traffic on this row:
        assert params[5] is None  # model
        assert params[8] is None  # latency_ms

    def test_first_transition_has_prev_phase_none(self) -> None:
        store = _MockStore()
        writer = PhaseTelemetryWriter(store)
        writer.phase_transition("t1", "intake", transitioned_by="cli")
        sql, params = store.calls[-1]
        assert params[4] == "phase_transition"
        assert params[16] is None  # prev_phase
        assert params[17] == "cli"  # transitioned_by


# ---------------------------------------------------------------------------
# T6 — soft-fail: if telemetry_store.execute() raises, method returns silently
# ---------------------------------------------------------------------------


class TestSoftFail:
    def test_execute_raises_returns_silently(self) -> None:
        """Any exception during _write must not propagate."""
        store = MagicMock()
        store.execute = MagicMock(side_effect=RuntimeError("db down"))
        writer = PhaseTelemetryWriter(store)

        # Should not raise
        writer.phase_start("t1", "p1")
        store.execute.assert_called()  # DDL attempt

    def test_ddl_raises_then_insert_raises(self) -> None:
        """Even when DDL fails, the caller sees nothing."""
        store = MagicMock()
        store.execute = MagicMock(side_effect=Exception("schema error"))
        writer = PhaseTelemetryWriter(store)

        writer.phase_complete("t1", "p1")
        # Called 7x: DDL + ADD COLUMN + ADD_WORKFLOW_DELIVERED + ADD PREV_PHASE
        # + ADD TRANSITIONED_BY + CREATE INDEX + INSERT, all fail silently
        assert store.execute.call_count == 7

    def test_llm_error_soft_fails(self) -> None:
        writer = PhaseTelemetryWriter(MagicMock(side_effect=OSError("disk full")))
        writer.llm_error("t1", "p1")  # noqa: PT017 — no assert; just doesn't raise


# ---------------------------------------------------------------------------
# PhaseEvent dataclass shape tests
# ---------------------------------------------------------------------------


class TestPhaseEventDataclass:
    def test_defaults(self) -> None:
        event = PhaseEvent(
            trace_id="t1",
            request_ts=1234,
            phase="compose",
            event_type="phase_start",
        )
        assert event.model is None
        assert event.tokens_in is None
        assert event.tokens_out is None
        assert event.latency_ms is None
        assert event.success is None
        assert event.error_message is None
        assert event.workflow_skill_id is None
        assert event.system_prompt_sha is None
        assert event.direction is None
        assert event.repo is None
        assert event.prev_phase is None
        assert event.transitioned_by is None

    def test_frozen(self) -> None:
        event = PhaseEvent(
            trace_id="t1",
            request_ts=1234,
            phase="compose",
            event_type="phase_start",
        )
        with pytest.raises(AttributeError):
            event.phase = "new"

    def test_all_fields(self) -> None:
        event = PhaseEvent(
            trace_id="t1",
            request_ts=1234,
            phase="compose",
            event_type="phase_complete",
            model="gpt-4",
            tokens_in=100,
            tokens_out=50,
            latency_ms=200,
            success=True,
            error_message=None,
            workflow_skill_id="skill-abc",
            system_prompt_sha="sha256:abc123",
            direction="upstream",
            repo="/home/nate/proj",
        )
        assert event.model == "gpt-4"
        assert event.tokens_in == 100
        assert event.tokens_out == 50
        assert event.latency_ms == 200
        assert event.success is True
        assert event.workflow_skill_id == "skill-abc"
        assert event.system_prompt_sha == "sha256:abc123"
        assert event.direction == "upstream"
        assert event.repo == "/home/nate/proj"


# ---------------------------------------------------------------------------
# Schema / DDL
# ---------------------------------------------------------------------------


class TestSchema:
    def test_create_ddl_contains_expected_table(self) -> None:
        from agentalloy.telemetry.phase_writer import _CREATE_DDL

        assert "phase_events" in _CREATE_DDL
        assert "CREATE TABLE IF NOT EXISTS phase_events" in _CREATE_DDL

    def test_create_ddl_contains_expected_indexes(self) -> None:
        from agentalloy.telemetry.phase_writer import _CREATE_DDL

        assert "idx_phase_events_ts" in _CREATE_DDL
        assert "idx_phase_events_phase" in _CREATE_DDL
        assert "idx_phase_events_type" in _CREATE_DDL
        assert "idx_phase_events_trace" in _CREATE_DDL

    def test_create_ddl_contains_repo_column(self) -> None:
        from agentalloy.telemetry.phase_writer import _CREATE_DDL

        assert "repo VARCHAR" in _CREATE_DDL

    def test_insert_sql_has_eighteen_placeholders(self) -> None:
        """Regression: the table has 18 columns (16 + prev_phase +
        transitioned_by); the INSERT must match exactly or every write raises
        (silently, under the soft-fail except block)."""
        from agentalloy.telemetry.phase_writer import _INSERT_SQL

        assert _INSERT_SQL.count("?") == 18

    def test_insert_sql_names_columns_explicitly(self) -> None:
        """INSERT INTO phase_events (col, col, ...) VALUES (...) — not a bare
        VALUES(...) — so column order cannot silently drift from the DDL again."""
        from agentalloy.telemetry.phase_writer import _INSERT_SQL

        assert "INSERT INTO phase_events (" in _INSERT_SQL


# ---------------------------------------------------------------------------
# Regression: real DuckDB round-trip (P0 bug — every write raised and was
# swallowed by the soft-fail except block; zero rows were ever persisted).
# ---------------------------------------------------------------------------


class TestRealDuckDBRoundTrip:
    def test_written_event_is_read_back(self, tmp_path: Path) -> None:
        from agentalloy.storage.telemetry_store import open_telemetry_store

        db_path = tmp_path / "telemetry.duck"
        store = open_telemetry_store(db_path)
        try:
            writer = PhaseTelemetryWriter(store)
            writer.llm_received(
                "trace-xyz",
                "design",
                model="qwen3-235b-a22b",
                tokens_out=2048,
                latency_ms=1500,
                success=True,
            )
            row = (
                store._c()
                .execute(  # noqa: SLF001 — direct read for the regression assertion
                    "SELECT trace_id, phase, event_type, model, tokens_out, latency_ms, success "
                    "FROM phase_events WHERE trace_id = ?",
                    ["trace-xyz"],
                )
                .fetchone()
            )
            assert row is not None
            assert row[0] == "trace-xyz"
            assert row[1] == "design"
            assert row[2] == "llm_received"
            assert row[3] == "qwen3-235b-a22b"
            assert row[4] == 2048
            assert row[5] == 1500
            assert row[6] is True
        finally:
            store.close()

    def test_repo_is_stored_and_read_back(self, tmp_path: Path) -> None:
        """Round-trip on a fresh DB: repo is persisted and readable."""
        from agentalloy.storage.telemetry_store import open_telemetry_store

        db_path = tmp_path / "telemetry_repo.duck"
        store = open_telemetry_store(db_path)
        try:
            writer = PhaseTelemetryWriter(store)
            writer.phase_start("trace-repo", "design", repo="/home/nate/proj-a")
            row = (
                store._c()
                .execute(  # noqa: SLF001 — direct read for the regression assertion
                    "SELECT repo FROM phase_events WHERE trace_id = ?",
                    ["trace-repo"],
                )
                .fetchone()
            )
            assert row is not None
            assert row[0] == "/home/nate/proj-a"
        finally:
            store.close()

    def test_phase_transition_is_stored_and_read_back(self, tmp_path: Path) -> None:
        from agentalloy.storage.telemetry_store import open_telemetry_store

        db_path = tmp_path / "telemetry_trans.duck"
        store = open_telemetry_store(db_path)
        try:
            writer = PhaseTelemetryWriter(store)
            writer.phase_transition(
                "trace-trans",
                "add-skill",
                prev_phase="intake",
                transitioned_by="f2ctl",
                repo="/home/nate/proj",
            )
            row = (
                store._c()
                .execute(  # noqa: SLF001 — direct read for the regression assertion
                    "SELECT trace_id, phase, event_type, prev_phase, transitioned_by, repo "
                    "FROM phase_events WHERE trace_id = ?",
                    ["trace-trans"],
                )
                .fetchone()
            )
            assert row is not None
            assert row[0] == "trace-trans"
            assert row[1] == "add-skill"
            assert row[2] == "phase_transition"
            assert row[3] == "intake"
            assert row[4] == "f2ctl"
            assert row[5] == "/home/nate/proj"
        finally:
            store.close()

    def test_multi_statement_ddl_creates_indexes(self, tmp_path: Path) -> None:
        """DuckDB's execute() runs every statement in the DDL string, not just
        the first — this asserts the indexes actually exist, not just the table."""
        from agentalloy.storage.telemetry_store import open_telemetry_store

        db_path = tmp_path / "telemetry2.duck"
        store = open_telemetry_store(db_path)
        try:
            writer = PhaseTelemetryWriter(store)
            writer.phase_start("t1", "spec")
            names = {
                r[0]
                for r in store._c()  # noqa: SLF001
                .execute(
                    "SELECT index_name FROM duckdb_indexes() WHERE table_name = 'phase_events'"
                )
                .fetchall()
            }
            assert names == {
                "idx_phase_events_ts",
                "idx_phase_events_phase",
                "idx_phase_events_type",
                "idx_phase_events_trace",
                "idx_phase_events_repo",
            }
        finally:
            store.close()

    def test_repo_index_created_on_fresh_db(self, tmp_path: Path) -> None:
        from agentalloy.storage.telemetry_store import open_telemetry_store

        db_path = tmp_path / "telemetry3.duck"
        store = open_telemetry_store(db_path)
        try:
            writer = PhaseTelemetryWriter(store)
            writer.phase_start("t1", "spec")
            names = {
                r[0]
                for r in store._c()  # noqa: SLF001
                .execute(
                    "SELECT index_name FROM duckdb_indexes() WHERE table_name = 'phase_events'"
                )
                .fetchall()
            }
            assert "idx_phase_events_repo" in names
        finally:
            store.close()


# ---------------------------------------------------------------------------
# Migration: a DB built between commit 9eb7eca and 74ac10b already has a
# 14-column phase_events table (CREATE TABLE IF NOT EXISTS was a no-op on it).
# _ensure_schema must self-heal by adding the repo column so writes on those
# pre-existing databases succeed instead of reproducing the silent-arity-
# failure bug that was just fixed.
# ---------------------------------------------------------------------------


class TestOldSchemaMigration:
    def _create_old_schema(self, db_path: Path) -> None:
        """Build a table matching the pre-#522 14-column schema exactly (no
        repo column) — the shape any DB built from 9eb7eca onward already has
        on disk."""
        import duckdb

        con = duckdb.connect(str(db_path))
        try:
            con.execute(
                """
                CREATE TABLE phase_events (
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
                    direction VARCHAR
                )
                """
            )
            cols = [r[0] for r in con.execute("DESCRIBE phase_events").fetchall()]
            assert len(cols) == 14, f"fixture drifted from the old schema: {cols}"
            assert "repo" not in cols
        finally:
            con.close()

    def test_write_succeeds_against_old_schema_and_repo_is_correct(self, tmp_path: Path) -> None:
        """The mandatory regression: instantiate the writer against an
        old-schema (14-column, no repo) database, write an event, and assert
        both that the write succeeded and the repo value round-trips."""
        from agentalloy.storage.telemetry_store import open_telemetry_store

        db_path = tmp_path / "old_schema.duck"
        self._create_old_schema(db_path)

        store = open_telemetry_store(db_path)
        try:
            writer = PhaseTelemetryWriter(store)
            writer.phase_start(
                "trace-migrated", "design", model="gpt-4", repo="/home/nate/old-repo"
            )

            row = (
                store._c()
                .execute(  # noqa: SLF001 — direct read for the regression assertion
                    "SELECT trace_id, phase, event_type, model, repo "
                    "FROM phase_events WHERE trace_id = ?",
                    ["trace-migrated"],
                )
                .fetchone()
            )
            assert row is not None, (
                "write against the old 14-column schema was silently swallowed "
                "by the soft-fail except block -- the exact bug #522 exists to prevent"
            )
            assert row[0] == "trace-migrated"
            assert row[1] == "design"
            assert row[2] == "phase_start"
            assert row[3] == "gpt-4"
            assert row[4] == "/home/nate/old-repo"

            cols = [r[0] for r in store._c().execute("DESCRIBE phase_events").fetchall()]  # noqa: SLF001
            assert "repo" in cols
        finally:
            store.close()

    def test_repo_index_created_after_migration(self, tmp_path: Path) -> None:
        """The repo index must be created AFTER the column migration runs —
        creating it against the DDL string directly would fail on an old
        table where the repo column doesn't exist yet."""
        from agentalloy.storage.telemetry_store import open_telemetry_store

        db_path = tmp_path / "old_schema_idx.duck"
        self._create_old_schema(db_path)

        store = open_telemetry_store(db_path)
        try:
            writer = PhaseTelemetryWriter(store)
            writer.phase_start("t1", "design")
            names = {
                r[0]
                for r in store._c()  # noqa: SLF001
                .execute(
                    "SELECT index_name FROM duckdb_indexes() WHERE table_name = 'phase_events'"
                )
                .fetchall()
            }
            assert "idx_phase_events_repo" in names
        finally:
            store.close()


# ---------------------------------------------------------------------------
# Migration: a DB built after the repo/workflow_delivered columns landed but
# before the phase_transition event has a 16-column phase_events table
# (no prev_phase / transitioned_by).  _ensure_schema must self-heal those
# too, or phase_transition writes on those databases are silently swallowed.
# ---------------------------------------------------------------------------


class TestIntermediateSchemaMigration:
    def _create_intermediate_schema(self, db_path: Path) -> None:
        """Build the 16-column schema exactly: 14 original + repo +
        workflow_delivered, but no prev_phase / transitioned_by."""
        import duckdb

        con = duckdb.connect(str(db_path))
        try:
            con.execute(
                """
                CREATE TABLE phase_events (
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
                    repo VARCHAR,
                    workflow_delivered BOOLEAN
                )
                """
            )
            cols = [r[0] for r in con.execute("DESCRIBE phase_events").fetchall()]
            assert len(cols) == 16, f"fixture drifted from the 16-column schema: {cols}"
            assert "prev_phase" not in cols
            assert "transitioned_by" not in cols
        finally:
            con.close()

    def test_phase_transition_succeeds_against_16_column_schema(self, tmp_path: Path) -> None:
        from agentalloy.storage.telemetry_store import open_telemetry_store

        db_path = tmp_path / "intermediate_schema.duck"
        self._create_intermediate_schema(db_path)

        store = open_telemetry_store(db_path)
        try:
            writer = PhaseTelemetryWriter(store)
            writer.phase_transition(
                "trace-mig",
                "build",
                prev_phase="spec",
                transitioned_by="sess-a",
                repo="/home/nate/mig",
            )

            row = (
                store._c()
                .execute(  # noqa: SLF001 — direct read for the regression assertion
                    "SELECT trace_id, phase, event_type, prev_phase, transitioned_by "
                    "FROM phase_events WHERE trace_id = ?",
                    ["trace-mig"],
                )
                .fetchone()
            )
            assert row is not None, (
                "phase_transition write against the 16-column schema was "
                "silently swallowed by the soft-fail except block"
            )
            assert row[2] == "phase_transition"
            assert row[3] == "spec"
            assert row[4] == "sess-a"

            cols = [r[0] for r in store._c().execute("DESCRIBE phase_events").fetchall()]  # noqa: SLF001
            assert "prev_phase" in cols
            assert "transitioned_by" in cols
        finally:
            store.close()
