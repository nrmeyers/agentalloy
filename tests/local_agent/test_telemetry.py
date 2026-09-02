"""LocalAgentTraceWriter (M1d) — one soft-failing row per finished ask.

Pins the row shape the /local-agent telemetry leg reads: stage latencies are
aggregated across steps, the DDL runs once per writer process, and a broken
store never fails the request path.
"""

from __future__ import annotations

import json
from typing import Any

from agentalloy.local_agent.loop import LoopResult, StepRecord
from agentalloy.local_agent.protocol import Action
from agentalloy.local_agent.telemetry import LocalAgentTraceWriter


class _FakeStore:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...] | None]] = []
        self.fail_next = False
        self.fail_insert = False

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("storage backend closed")
        if self.fail_insert and "INSERT" in sql:
            raise RuntimeError("storage backend closed")
        self.executed.append((sql, params))

    @property
    def ddl_statements(self) -> list[str]:
        return [sql for sql, _ in self.executed if "CREATE TABLE" in sql]

    @property
    def insert_statements(self) -> list[tuple[Any, ...]]:
        return [params for sql, params in self.executed if "INSERT" in sql]


def _result(
    *actions: Action,
    stop_reason: str = "none",
    degraded: bool = False,
    degrade_reason: str | None = None,
    answer: str = "the answer",
) -> LoopResult:
    steps = [
        StepRecord(
            step=i + 1,
            action=action.value,
            args={},
            validation="ok",
            result_chars=10,
            stage_latencies_ms={"fill": 7, "execute": 2},
        )
        for i, action in enumerate(actions)
    ]
    return LoopResult(
        answer=answer,
        steps=steps,
        stop_reason=stop_reason,
        degraded=degraded,
        degrade_reason=degrade_reason,
        model_tag="lfm2.5-2.6b-compressor",
        total_ms=12,
    )


class TestRecordRow:
    def test_one_row_with_aggregated_stage_latencies(self) -> None:
        store = _FakeStore()
        writer = LocalAgentTraceWriter(store)
        writer.record(
            result=_result(Action.CODE_SEARCH, Action.NONE),
            question="where is the auth bug?",
            phase="build",
            repo="main-repo",
        )

        assert len(store.ddl_statements) == 1
        assert len(store.insert_statements) == 1
        params = store.insert_statements[0]
        assert len(params) == 13
        (
            trace_id,
            request_ts,
            phase,
            repo,
            question,
            model_tag,
            stop_reason,
            degraded,
            degrade_reason,
            steps_count,
            actions,
            stage_latency_ms,
            total_ms,
        ) = params
        assert len(trace_id) == 32  # uuid4().hex
        assert isinstance(request_ts, int) and request_ts > 0
        assert phase == "build"
        assert repo == "main-repo"
        assert question == "where is the auth bug?"
        assert model_tag == "lfm2.5-2.6b-compressor"
        assert stop_reason == "none"
        assert degraded is False
        assert degrade_reason is None
        assert steps_count == 2
        assert actions == ["code_search", "none"]
        # Both steps contribute their per-stage latencies.
        assert json.loads(stage_latency_ms) == {"fill": 14, "execute": 4}
        assert total_ms == 12

    def test_optional_fields_default_to_none(self) -> None:
        store = _FakeStore()
        writer = LocalAgentTraceWriter(store)
        writer.record(result=_result(Action.NONE), question="q")
        params = store.insert_statements[0]
        assert params[2] is None  # phase
        assert params[3] is None  # repo

    def test_degraded_row_carries_the_reason(self) -> None:
        store = _FakeStore()
        writer = LocalAgentTraceWriter(store)
        writer.record(
            result=_result(
                stop_reason="validation_failed",
                degraded=True,
                degrade_reason="validation_failed: x",
            ),
            question="q",
        )
        params = store.insert_statements[0]
        assert params[6] == "validation_failed"
        assert params[7] is True
        assert params[8] == "validation_failed: x"


class TestSchemaLifecycle:
    def test_ddl_runs_once_per_writer_process(self) -> None:
        store = _FakeStore()
        writer = LocalAgentTraceWriter(store)
        writer.record(result=_result(Action.NONE), question="q1")
        writer.record(result=_result(Action.NONE), question="q2")
        assert len(store.ddl_statements) == 1
        assert len(store.insert_statements) == 2

    def test_two_writers_each_run_ddl(self) -> None:
        store = _FakeStore()
        LocalAgentTraceWriter(store).record(result=_result(Action.NONE), question="q1")
        LocalAgentTraceWriter(store).record(result=_result(Action.NONE), question="q2")
        assert len(store.ddl_statements) == 2

    def test_ddl_creates_table_and_indexes(self) -> None:
        store = _FakeStore()
        LocalAgentTraceWriter(store).record(result=_result(Action.NONE), question="q")
        ddl = store.ddl_statements[0]
        assert "CREATE TABLE IF NOT EXISTS local_agent_traces" in ddl
        assert "idx_local_agent_traces_ts" in ddl
        assert "idx_local_agent_traces_repo" in ddl


class TestSoftFailure:
    def test_insert_failure_never_raises(self) -> None:
        store = _FakeStore()
        writer = LocalAgentTraceWriter(store)
        store.fail_insert = True
        writer.record(result=_result(Action.NONE), question="q")  # must not raise
        assert store.insert_statements == []  # the insert was attempted, not recorded
        assert len(store.ddl_statements) == 1  # the schema ran first and succeeded

    def test_ddl_failure_still_attempts_insert(self) -> None:
        store = _FakeStore()
        store.fail_next = True  # the DDL is the first execute
        writer = LocalAgentTraceWriter(store)
        writer.record(result=_result(Action.NONE), question="q")  # must not raise
        assert len(store.ddl_statements) == 0
        assert len(store.insert_statements) == 1
        # The latch is set even on DDL failure — no retry storm.
        writer.record(result=_result(Action.NONE), question="q2")
        assert len(store.ddl_statements) == 0
        assert len(store.insert_statements) == 2
