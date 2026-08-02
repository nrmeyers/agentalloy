"""Tests for the phase-event telemetry writer (Task 01 of add-telemetry)."""

from __future__ import annotations

import time
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

        assert len(store.calls) == 2  # DDL + INSERT
        sql, params = store.calls[1]
        assert "INSERT INTO phase_events" in sql
        assert params[0] == trace_id  # trace_id
        assert params[3] == phase  # phase
        assert params[4] == "phase_start"  # event_type
        assert params[5] == "gpt-4"  # model
        assert params[6] == 100  # tokens_in
        assert params[7] == 50  # tokens_out
        assert params[10] is None  # success
        assert params[11] is None  # error_message

    def test_success_flag_is_written(self) -> None:
        store = _MockStore()
        writer = PhaseTelemetryWriter(store)
        writer.phase_start("t1", "p1", success=True)
        sql, params = store.calls[1]
        assert params[9] is True  # success column

    def test_correlation_id_is_passed_through(self) -> None:
        store = _MockStore()
        writer = PhaseTelemetryWriter(store)
        writer.phase_start("t1", "p1", correlation_id="corr-1")
        sql, params = store.calls[1]
        assert params[1] == "corr-1"  # correlation_id column


# ---------------------------------------------------------------------------
# T2 — phase_complete writes correct row
# ---------------------------------------------------------------------------


class TestPhaseComplete:
    def test_writes_correct_row(self) -> None:
        store = _MockStore()
        writer = PhaseTelemetryWriter(store)
        writer.phase_complete("t1", "compose", latency_ms=250, success=True)
        sql, params = store.calls[1]
        assert params[4] == "phase_complete"
        assert params[8] == 250  # latency_ms
        assert params[9] is True  # success

    def test_error_message_is_written(self) -> None:
        store = _MockStore()
        writer = PhaseTelemetryWriter(store)
        writer.phase_complete("t1", "assemble", error_message="timeout")
        sql, params = store.calls[1]
        assert params[10] == "timeout"


# ---------------------------------------------------------------------------
# T3 — llm_sent writes correct row
# ---------------------------------------------------------------------------


class TestLlmSent:
    def test_writes_correct_row(self) -> None:
        store = _MockStore()
        writer = PhaseTelemetryWriter(store)
        writer.llm_sent("t1", "llm", model="claude-sonnet-4-5-20250514", tokens_out=300)
        sql, params = store.calls[1]
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
        sql, params = store.calls[1]
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
        sql, params = store.calls[1]
        assert params[4] == "llm_error"
        assert params[10] == "rate limit"
        assert params[9] is False


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
        # Called twice: DDL + INSERT, both fail silently
        assert store.execute.call_count == 2

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
        )
        assert event.model == "gpt-4"
        assert event.tokens_in == 100
        assert event.tokens_out == 50
        assert event.latency_ms == 200
        assert event.success is True
        assert event.workflow_skill_id == "skill-abc"
        assert event.system_prompt_sha == "sha256:abc123"
        assert event.direction == "upstream"


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

    def test_insert_sql_has_thirteen_placeholders(self) -> None:
        from agentalloy.telemetry.phase_writer import _INSERT_SQL

        assert _INSERT_SQL.count("?") == 13
