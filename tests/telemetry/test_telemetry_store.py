"""Unit tests for the DuckDB TelemetryStore (v5 storage layer).

Covers the analytics output contract (D15): aggregate_savings totals + shape,
the 40-field query_traces roundtrip, and clear_telemetry counts.
"""

from __future__ import annotations

import pytest

from agentalloy.storage import CompositionTrace
from agentalloy.storage.telemetry_store import open_telemetry_store


@pytest.fixture
def store(tmp_path):
    ts = open_telemetry_store(str(tmp_path / "telemetry.duck"))
    yield ts
    ts.close()


def _trace(tid: str, ts_: int, phase: str, ret: int, flat: int, **kw) -> CompositionTrace:
    return CompositionTrace(
        trace_id=tid,
        request_ts=ts_,
        phase=phase,
        task_prompt="p",
        status=kw.pop("status", "proxy_composed"),
        tokens_returned=ret,
        tokens_flat_equivalent=flat,
        **kw,
    )


def test_record_and_count(store):
    store.record_composition_trace(_trace("t1", 1, "build", 100, 400))
    store.record_composition_trace(_trace("t2", 2, "qa", 50, 200))
    assert store.count_traces() == 2
    assert store.count_traces_filtered(phase="build") == 1
    assert store.count_traces_filtered(status="proxy_composed") == 2


def test_query_roundtrip_newest_first(store):
    store.record_composition_trace(
        _trace("t1", 1, "build", 100, 400, lm_assist_kept_ids=["a"], lm_assist_scores='{"a":0.9}')
    )
    store.record_composition_trace(_trace("t2", 2, "qa", 50, 200))
    q = store.query_traces(limit=10)
    assert [t.trace_id for t in q] == ["t2", "t1"]  # ORDER BY request_ts DESC
    assert q[1].lm_assist_kept_ids == ["a"]
    assert q[1].lm_assist_scores == '{"a":0.9}'
    assert q[1].tokens_flat_equivalent == 400


def test_aggregate_savings_contract(store):
    store.record_composition_trace(_trace("t1", 1, "build", 100, 400))
    store.record_composition_trace(_trace("t2", 2, "qa", 50, 200))
    agg = store.aggregate_savings()
    assert agg["total_composes"] == 2
    assert agg["tokens_returned"] == 150
    assert agg["tokens_flat_equivalent"] == 600
    assert agg["tokens_saved"] == 450
    assert agg["savings_pct"] == round(450 / 600 * 100, 1)
    assert {p["phase"] for p in agg["per_phase"]} == {"build", "qa"}


def test_aggregate_savings_repo_scope(store):
    store.record_composition_trace(_trace("t1", 1, "build", 100, 400, repo="/a"))
    store.record_composition_trace(_trace("t2", 2, "build", 10, 40, repo="/b"))
    assert store.aggregate_savings(repo="/a")["total_composes"] == 1
    # nested path counts under the repo root
    store.record_composition_trace(_trace("t3", 3, "build", 5, 20, repo="/a/sub"))
    assert store.aggregate_savings(repo="/a")["total_composes"] == 2


def test_clear_telemetry(store):
    store.record_composition_trace(_trace("t1", 1, "build", 100, 400))
    assert store.clear_telemetry() == {"traces_deleted": 1}
    assert store.count_traces() == 0


def test_savings_pct_zero_when_no_flat(store):
    store.record_composition_trace(_trace("t1", 1, "build", 0, 0))
    assert store.aggregate_savings()["savings_pct"] == 0.0


def test_query_traces_repo_filter(store):
    store.record_composition_trace(_trace("t1", 1, "build", 1, 4, repo="/a"))
    store.record_composition_trace(_trace("t2", 2, "build", 1, 4, repo="/a/sub"))
    store.record_composition_trace(_trace("t3", 3, "build", 1, 4, repo="/b"))
    assert {t.trace_id for t in store.query_traces(repo="/a")} == {"t1", "t2"}
    assert store.count_traces_filtered(repo="/a") == 2


def test_aggregate_coverage_contract(store):
    store.record_composition_trace(_trace("t1", 1, "build", 100, 400, repo="/a"))
    store.record_composition_trace(
        _trace("t2", 2, "build", 0, 0, status="proxy_passthrough", repo="/a")
    )
    store.record_composition_trace(
        _trace("t3", 3, "qa", 0, 0, status="proxy_passthrough", repo="/b")
    )
    # Non-proxy rows (e.g. direct /compose) don't count toward coverage.
    store.record_composition_trace(_trace("t4", 4, "build", 10, 40, status="compose"))

    agg = store.aggregate_coverage()
    assert agg["total"] == 3
    assert agg["composed"] == 1
    assert agg["passthrough"] == 2
    assert agg["compose_rate"] == round(1 / 3 * 100, 1)
    by_phase = {p["phase"]: p for p in agg["per_phase"]}
    assert by_phase["build"] == {"phase": "build", "composed": 1, "passthrough": 1}
    assert by_phase["qa"] == {"phase": "qa", "composed": 0, "passthrough": 1}
    by_repo = {r["repo"]: r for r in agg["per_repo"]}
    assert by_repo["/a"]["composed"] == 1

    scoped = store.aggregate_coverage(repo="/a")
    assert scoped["total"] == 2
    assert scoped["compose_rate"] == 50.0


def test_aggregate_coverage_empty(store):
    agg = store.aggregate_coverage()
    assert agg == {
        "total": 0,
        "composed": 0,
        "passthrough": 0,
        "compose_rate": 0.0,
        "per_phase": [],
        "per_repo": [],
    }
