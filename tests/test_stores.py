"""Store tests — verify DuckDB persistence (T7)."""

import tempfile
from pathlib import Path

from agentalloy.state_store import StateStore
from agentalloy.telemetry_store import TelemetryStore


def test_state_store_contract_lifecycle() -> None:
    """Contract add → get → update."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "state.duck")
        store = StateStore(db_path)

        # Add contract
        store.add_contract("test-contract", ["domain1", "domain2"], "test touches")

        # Get contract
        contract = store.get_contract("test-contract")
        assert contract is not None
        assert contract["slug"] == "test-contract"
        assert contract["domain_tags"] == ["domain1", "domain2"]
        assert contract["touches"] == "test touches"

        # Update contract
        store.add_contract("test-contract", ["updated"], "new touches")
        contract = store.get_contract("test-contract")
        assert contract["domain_tags"] == ["updated"]
        assert contract["touches"] == "new touches"

        store.close()


def test_state_store_artifact_lifecycle() -> None:
    """Artifact record → get."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "state.duck")
        store = StateStore(db_path)

        # Record artifact
        store.record_artifact("spec", "test-artifact", "artifact body content")

        # Get artifact
        body = store.get_artifact("spec", "test-artifact")
        assert body == "artifact body content"

        # Update artifact
        store.record_artifact("spec", "test-artifact", "updated body")
        body = store.get_artifact("spec", "test-artifact")
        assert body == "updated body"

        store.close()


def test_state_store_phase_advance() -> None:
    """Phase advance."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "state.duck")
        store = StateStore(db_path)

        # New stores start at intake, the lifecycle front door
        assert store.get_current_phase() == "intake"

        # Advance
        store.advance_phase("design")
        assert store.get_current_phase() == "design"

        store.close()


def test_state_store_advance_phase_rejects_unknown() -> None:
    """Fail-closed write: an unknown phase can never be persisted."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "state.duck")
        store = StateStore(db_path)

        try:
            store.advance_phase("not-a-phase")
            raise AssertionError("expected ValueError")
        except ValueError as e:
            assert "not-a-phase" in str(e)
            assert "intake" in str(e)  # legal phases listed

        # Store unchanged
        assert store.get_current_phase() == "intake"

        store.close()


def test_state_store_reset_phase() -> None:
    """reset_phase: back to intake, approvals cleared, knowledge kept."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "state.duck")
        store = StateStore(db_path)

        # Build up lifecycle state
        store.advance_phase("plan")
        digest = store.record_artifact("spec", "spec-exit", "done")
        store.record_approval("spec→design", digest)
        store.add_contract("keep-me", ["python"], "touches")

        # Reset
        phase = store.reset_phase()
        assert phase == "intake"
        assert store.get_current_phase() == "intake"

        # Approvals gone, knowledge kept
        assert not store.is_approved("spec→design", digest)
        assert store.get_artifact("spec", "spec-exit") == "done"
        assert store.get_contract("keep-me") is not None

        store.close()


def test_telemetry_store_trace_lifecycle() -> None:
    """Trace record → get."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "telemetry.duck")
        store = TelemetryStore(db_path)

        # Record traces
        id1 = store.record_trace("session1", 0, "code_search", '{"query": "test"}', "result1")
        id2 = store.record_trace(
            "session1", 1, "symbols", '{"fqn": "test.Func"}', "result2", phase="spec"
        )

        assert id1 > 0
        assert id2 > 0

        # Get all traces
        traces = store.get_traces(k=10)
        assert len(traces) == 2
        assert traces[0]["tool_name"] == "symbols"  # Most recent first

        # Get traces by phase
        traces = store.get_traces(k=10, phase="spec")
        assert len(traces) == 1
        assert traces[0]["tool_name"] == "symbols"

        store.close()


def test_telemetry_record_run_and_analytics() -> None:
    """record_run writes one row per tool call + a summary row carrying the
    stop_reason; analytics exclude the summary row from tool usage."""
    import tempfile

    from agentalloy import executors

    with tempfile.TemporaryDirectory() as tmpdir:
        store = TelemetryStore(str(Path(tmpdir) / "telemetry.duck"))
        store.record_run(
            "sess-a",
            [
                {"tool": "code_search", "args": '{"query": "x"}', "result": "r1"},
                {"tool": "contract_add", "args": '{"slug": "s"}', "result": "r2"},
            ],
            stop_reason="answer",
            phase="build",
        )

        usage = store.tool_usage_summary()
        assert usage["total_calls"] == 2
        assert {t["name"] for t in usage["tools"]} == {"code_search", "contract_add"}

        reasons = store.stop_reason_distribution()["reasons"]
        assert reasons == [{"reason": "answer", "count": 1}]

        phases = store.phase_activity()["phases"]
        assert phases[0]["phase"] == "build"

        # The `telemetry` tool serves real traces once the store is injected.
        executors.set_telemetry(store)
        try:
            import json

            out = json.loads(executors.execute_tool("telemetry", '{"k": 5}'))
            assert len(out["traces"]) == 3  # 2 calls + 1 summary row
        finally:
            executors.set_telemetry(None)
        store.close()


def test_project_scoping_isolates_lifecycle_state() -> None:
    """Scoped views isolate phase/contracts/artifacts/approvals per project;
    the legacy '' scope keeps pre-scoping behavior."""
    with tempfile.TemporaryDirectory() as tmp:
        store = StateStore(str(Path(tmp) / "state.duck"))
        a = store.scoped("alpha-11111111")
        b = store.scoped("beta-22222222")

        a.add_contract("invoice-export", ["finance"], "invoices")
        a.advance_phase("spec")

        # New scope starts at lifecycle start with no contracts.
        assert b.get_current_phase() == "intake"
        assert b.list_contracts() == []
        assert b.get_contract("invoice-export") is None
        # Legacy/global scope untouched.
        assert store.get_current_phase() == "intake"
        assert store.get_contract("invoice-export") is None
        # Same slug can exist per-project.
        b.add_contract("invoice-export", ["x"], "other")
        assert a.get_contract("invoice-export")["touches"] == "invoices"
        assert b.get_contract("invoice-export")["touches"] == "other"

        # Artifacts + approvals scope too.
        a.record_artifact("spec", "spec-exit", "body-a")
        assert b.get_artifact("spec", "spec-exit") is None
        a.record_approval("spec→design", "digest-a")
        assert not b.is_approved("spec→design", "digest-a")

        # reset_phase clears only the scope's approvals.
        b.record_approval("spec→design", "digest-b")
        a.reset_phase()
        assert b.is_approved("spec→design", "digest-b")
        store.close()


def test_preproject_schema_migrates_to_legacy_scope() -> None:
    """A pre-project-scope database migrates in place: old rows land in the
    '' scope, and reopening is idempotent."""
    import duckdb

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "state.duck"
        conn = duckdb.connect(str(db))
        conn.execute(
            "CREATE TABLE contracts (slug TEXT PRIMARY KEY, domain_tags TEXT, touches TEXT, "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
            "updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute("INSERT INTO contracts (slug, domain_tags, touches) VALUES ('c1', '[]', 't')")
        conn.execute(
            "CREATE TABLE phases (id INTEGER PRIMARY KEY DEFAULT 1, "
            "current_phase TEXT DEFAULT 'intake', updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute("INSERT INTO phases (id, current_phase) VALUES (1, 'ship')")
        conn.close()

        store = StateStore(str(db))
        assert store.get_current_phase() == "ship"
        assert store.get_contract("c1") is not None
        assert store.scoped("new-proj-12345678").get_current_phase() == "intake"
        store.close()

        reopened = StateStore(str(db))
        assert reopened.get_current_phase() == "ship"
        reopened.close()
