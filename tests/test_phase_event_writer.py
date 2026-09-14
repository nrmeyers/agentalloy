"""PhaseTelemetryWriter — schema, persistence, and soft-fail contract.

Covers the gate that ``phase_events`` carries token counts (``tokens_in`` /
``tokens_out``) so a composition can be triaged from telemetry alone
(prompt-too-big = large ``tokens_in``, not small ``tokens_out``).
"""

from __future__ import annotations

import duckdb

from agentalloy.telemetry.phase_writer import PhaseTelemetryWriter


class _BrokenStore:
    def execute(self, sql: str, params=None) -> None:
        raise RuntimeError("store down")


def _writer(tmp_path) -> tuple[PhaseTelemetryWriter, duckdb.DuckDBPyConnection]:
    conn = duckdb.connect(str(tmp_path / "telemetry.duck"))
    return PhaseTelemetryWriter(conn), conn


def test_schema_has_token_columns(tmp_path):
    writer, conn = _writer(tmp_path)
    writer.llm_sent("t1", "specced", model="m", tokens_in=100, tokens_out=10)
    cols = {r[0] for r in conn.execute("DESCRIBE phase_events").fetchall()}
    assert "tokens_in" in cols
    assert "tokens_out" in cols
    conn.close()


def test_llm_sent_persists_tokens(tmp_path):
    writer, conn = _writer(tmp_path)
    writer.llm_sent("t1", "specced", model="m", tokens_in=42_000, tokens_out=1_234)
    row = conn.execute(
        "SELECT tokens_in, tokens_out, event_type FROM phase_events WHERE trace_id = 't1'"
    ).fetchone()
    assert row == (42_000, 1_234, "llm_sent")
    conn.close()


def test_tokens_null_when_unknown(tmp_path):
    writer, conn = _writer(tmp_path)
    # A call site that only knows the output size leaves tokens_in NULL —
    # the dashboard must read that as "unknown", not zero.
    writer.llm_received("t1", "specced", model="m", tokens_out=77, latency_ms=120)
    row = conn.execute(
        "SELECT tokens_in, tokens_out FROM phase_events WHERE event_type = 'llm_received'"
    ).fetchone()
    assert row[0] is None
    assert row[1] == 77
    conn.close()


def test_phase_transition_carries_provenance_not_tokens(tmp_path):
    writer, conn = _writer(tmp_path)
    writer.phase_transition(
        "t1",
        "specced",
        prev_phase="intake",
        transitioned_by="api",
        workflow_skill_id="wf",
        workflow_delivered=True,
    )
    row = conn.execute(
        "SELECT event_type, prev_phase, transitioned_by, workflow_delivered, "
        "tokens_in, tokens_out, model FROM phase_events WHERE trace_id = 't1'"
    ).fetchone()
    assert row == ("phase_transition", "intake", "api", True, None, None, None)
    conn.close()


def test_legacy_table_migrates_upwards(tmp_path):
    """A 14-column table (pre-repo/workflow_delivered/prev_phase/transitioned_by)
    is healed by the self-migrations on first write and accepts the full row."""
    conn = duckdb.connect(str(tmp_path / "telemetry.duck"))
    conn.execute(
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
        )
        """
    )
    conn.close()

    writer, conn = _writer(tmp_path)
    writer.phase_transition(
        "t1",
        "specced",
        model="m",
        tokens_in=5,
        tokens_out=6,
        prev_phase="intake",
        transitioned_by="api",
        workflow_delivered=True,
    )
    row = conn.execute(
        "SELECT tokens_in, tokens_out, repo, workflow_delivered, prev_phase, "
        "transitioned_by FROM phase_events WHERE trace_id = 't1'"
    ).fetchone()
    assert row == (5, 6, None, True, "intake", "api")
    conn.close()


def test_soft_fail_never_propagates():
    writer = PhaseTelemetryWriter(_BrokenStore())
    # Must not raise — telemetry must not take the caller down.
    writer.llm_sent("t1", "specced", tokens_in=1)
    writer.phase_complete("t1", "specced")
    writer.phase_transition("t1", "specced", prev_phase="intake")
