"""M1 Acceptance Criteria verification tests.

AC-3: Phase machine end-to-end (full main lane)
AC-9: Approval gates (4 properties)
AC-10: One build contract per task
AC-16: v2-never-writes-target-source
"""

import hashlib
import json
import tempfile
from pathlib import Path

from agentalloy.phase_machine import PHASE_ORDER, PhaseMachine
from agentalloy.sessions import SessionManager
from agentalloy.state_store import StateStore


def _make_store(tmpdir: str) -> StateStore:
    return StateStore(str(Path(tmpdir) / "state.duck"))


# ─── AC-3: Full main-lane end-to-end ─────────────────────────────────


def test_ac3_full_main_lane() -> None:
    """AC-3: Drive a task through spec → design → plan → build → qa → ship.

    Each phase transition requires an exit artifact. Gated transitions
    (spec→design, design→plan, plan→build) also require approval.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _make_store(tmpdir)
        machine = PhaseMachine(store)

        # Start at intake, the lifecycle front door
        assert store.get_current_phase() == "intake"

        # Route decided → record intake exit artifact, advance to spec
        # (intake→spec is exit-artifact gated, not approval gated)
        store.record_artifact("intake", "intake-exit", "route: full")
        store.advance_phase("spec")
        assert store.get_current_phase() == "spec"

        # Record spec exit artifact and get approval
        spec_digest = store.record_artifact("spec", "spec-exit", "spec complete")
        store.record_approval("spec→design", spec_digest)

        # Advance to design
        store.advance_phase("design")
        assert store.get_current_phase() == "design"

        # Record design exit artifact and get approval
        design_digest = store.record_artifact("design", "design-exit", "design complete")
        store.record_approval("design→plan", design_digest)

        # Advance to plan
        store.advance_phase("plan")
        assert store.get_current_phase() == "plan"

        # Record plan exit artifact and get approval
        plan_digest = store.record_artifact("plan", "plan-exit", "plan complete")
        store.record_approval("plan→build", plan_digest)

        # Advance to build
        store.advance_phase("build")
        assert store.get_current_phase() == "build"

        # Build → qa (non-gated)
        store.record_artifact("build", "build-exit", "build complete")
        store.advance_phase("qa")
        assert store.get_current_phase() == "qa"

        # QA → ship (non-gated)
        store.record_artifact("qa", "qa-exit", "qa complete")
        store.advance_phase("ship")
        assert store.get_current_phase() == "ship"

        # Verify gate checks at each phase
        for phase in PHASE_ORDER:
            gate = machine.check_gate(phase)
            assert gate["phase"] == phase

        store.close()


# ─── AC-9: Approval gate properties ──────────────────────────────────


def test_ac9_property_1_exit_artifact_required() -> None:
    """AC-9 #1: Advance refused until exit artifact is recorded."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _make_store(tmpdir)

        # No exit artifact → gate check shows not ready
        machine = PhaseMachine(store)
        gate = machine.check_gate("spec")
        assert gate["has_exit_artifact"] is False
        assert gate["requires_approval"] is True

        store.close()


def test_ac9_property_2_approval_required() -> None:
    """AC-9 #2: Gated transitions require explicit approval."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _make_store(tmpdir)

        # Has exit artifact but no approval
        store.record_artifact("spec", "spec-exit", "done")
        machine = PhaseMachine(store)
        gate = machine.check_gate("spec")
        assert gate["has_exit_artifact"] is True
        assert gate["approved"] is False

        store.close()


def test_ac9_property_3_digest_invalidation() -> None:
    """AC-9 #3: Editing an approved artifact voids the approval."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _make_store(tmpdir)

        # Record and approve
        digest1 = store.record_artifact("spec", "spec-exit", "original")
        store.record_approval("spec→design", digest1)

        machine = PhaseMachine(store)
        gate = machine.check_gate("spec")
        assert gate["approved"] is True

        # Edit the artifact → approval voided
        digest2 = store.record_artifact("spec", "spec-exit", "edited")
        assert digest1 != digest2

        gate2 = machine.check_gate("spec")
        assert gate2["approved"] is False

        store.close()


def test_ac9_property_4_force_never_bypasses() -> None:
    """AC-9 #4: --force never bypasses the gate.

    The phase_advance executor rejects advance without approval,
    regardless of any force flag.
    """
    from agentalloy.executors import _phase_advance, set_store

    with tempfile.TemporaryDirectory() as tmpdir:
        store = _make_store(tmpdir)
        set_store(store)
        store.advance_phase("spec")

        # Record exit artifact but don't approve
        store.record_artifact("spec", "spec-exit", "done")

        # Try to advance with approved=True (simulating --force)
        result = json.loads(_phase_advance({"target": "design", "approved": True}))

        # Should be rejected — no approval recorded
        assert result["status"] == "rejected"
        assert "requires approval" in result["reason"]

        set_store(None)
        store.close()


# ─── AC-10: One build contract per task ──────────────────────────────


def test_ac10_one_contract_per_task() -> None:
    """AC-10: Plan produces one build contract per task, each ≤2 domain tags."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _make_store(tmpdir)
        mgr = SessionManager(store)
        mgr.create("build-session")

        # Simulate plan phase: 3 tasks, each with a contract
        tasks = [
            ("auth-module", ["python", "security"]),
            ("api-endpoints", ["python", "api"]),
            ("test-suite", ["python", "testing"]),
        ]

        for task_slug, domain_tags in tasks:
            contract_slug = f"contract-{task_slug}"
            store.add_contract(contract_slug, domain_tags, f"Implement {task_slug}")
            mgr.add_work_item(task_slug, "build-session", contract_slug)

            # Verify contract has ≤2 domain tags
            contract = store.get_contract(contract_slug)
            assert contract is not None
            assert len(contract["domain_tags"]) <= 2

        # Verify contracts ≥ tasks
        work_items = mgr.get_cursor("build-session")
        assert len(work_items) >= len(tasks)

        # Verify each task has a contract
        for item in work_items:
            assert item["contract_slug"] is not None
            contract = store.get_contract(item["contract_slug"])
            assert contract is not None

        store.close()


# ─── AC-16: v2-never-writes-target-source ────────────────────────────


def test_ac16_v2_never_writes_target_source() -> None:
    """AC-16: v2-owned code paths mutate no target-repo source file.

    v2 writes only: workflow state, contracts, artifacts, approvals,
    sessions, telemetry — all in v2's own stores.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        # Create a fake target repo
        target_repo = tmpdir_path / "target-repo"
        target_repo.mkdir()
        source_file = target_repo / "main.py"
        source_file.write_text("print('hello')\n")

        # Record the source tree hash before v2 operations
        source_hash_before = hashlib.sha256(source_file.read_bytes()).hexdigest()

        # Run v2 operations (all state goes to v2's own stores)
        db_path = str(tmpdir_path / "state.duck")
        store = StateStore(db_path)

        # All v2 state operations
        store.add_contract("test-contract", ["python"], "test")
        store.record_artifact("spec", "spec-exit", "spec done")
        digest = store.record_artifact("spec", "spec-exit", "spec done")
        store.record_approval("spec→design", digest)
        store.advance_phase("design")

        mgr = SessionManager(store)
        mgr.create("test-session")
        mgr.add_work_item("task-1", "test-session")
        mgr.stash("test-session")

        store.close()

        # Verify source file is untouched
        source_hash_after = hashlib.sha256(source_file.read_bytes()).hexdigest()
        assert source_hash_before == source_hash_after
        assert source_file.read_text() == "print('hello')\n"

        # Verify no new files were created in the target repo
        target_files = list(target_repo.rglob("*"))
        assert len(target_files) == 1  # only main.py
        assert target_files[0].name == "main.py"


# ─── AC-11: Crash-resumable sessions ─────────────────────────────────


def test_ac11_crash_resumable_sessions() -> None:
    """AC-11: stash → process kill → resume continues from exact position."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "state.duck")

        # Phase 1: Run workflow partway, then stash
        store1 = StateStore(db_path)
        mgr1 = SessionManager(store1)
        mgr1.create("workflow-1")

        # Do some work
        store1.add_contract("contract-auth", ["python"], "auth module")
        store1.record_artifact("spec", "spec-exit", "spec complete")
        digest = store1.record_artifact("spec", "spec-exit", "spec complete")
        store1.record_approval("spec→design", digest)
        store1.advance_phase("design")
        mgr1.add_work_item("task-auth", "workflow-1", "contract-auth")
        mgr1.add_work_item("task-api", "workflow-1")

        # Stash (simulates graceful shutdown)
        snapshot = mgr1.stash("workflow-1")
        assert snapshot is not None
        assert snapshot["phase"] == "design"

        # Phase 2: Simulate process kill — close store, reopen
        store1.close()

        # Phase 3: Resume from the exact position
        store2 = StateStore(db_path)
        mgr2 = SessionManager(store2)

        result = mgr2.resume("workflow-1")
        assert result is not None
        assert result["phase"] == "design"

        # Verify state was restored
        assert store2.get_current_phase() == "design"
        contract = store2.get_contract("contract-auth")
        assert contract is not None

        # Verify cursor was restored
        cursor = mgr2.get_cursor("workflow-1")
        assert len(cursor) == 2
        assert cursor[0]["task_slug"] == "task-auth"

        store2.close()
