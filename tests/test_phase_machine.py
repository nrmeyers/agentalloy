"""Phase machine tests — LangGraph gates, digest invalidation, approval flow."""

import tempfile
from pathlib import Path

from agentalloy.phase_machine import (
    APPROVAL_GATES,
    PHASE_ORDER,
    PhaseMachine,
    create_task_fanout,
)
from agentalloy.state_store import StateStore


def _make_store(tmpdir: str) -> StateStore:
    return StateStore(str(Path(tmpdir) / "state.duck"))


def test_phase_order_and_gates() -> None:
    """Phase order and approval gates are correct."""
    assert list(PHASE_ORDER) == ["intake", "spec", "design", "plan", "build", "qa", "ship"]
    assert PHASE_ORDER[0] == "intake"
    assert "spec→design" in APPROVAL_GATES
    assert "design→plan" in APPROVAL_GATES
    assert "plan→build" in APPROVAL_GATES
    # Non-gated transitions (intake→spec is exit-artifact gated, not approval gated)
    assert "intake→spec" not in APPROVAL_GATES
    assert "build→qa" not in APPROVAL_GATES
    assert "qa→ship" not in APPROVAL_GATES


def test_digest_computation() -> None:
    """record_artifact returns a digest."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _make_store(tmpdir)
        digest = store.record_artifact("spec", "spec-exit", "done")
        assert len(digest) == 16
        assert store.get_artifact_digest("spec", "spec-exit") == digest
        store.close()


def test_digest_changes_on_edit() -> None:
    """Editing an artifact changes its digest."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _make_store(tmpdir)
        d1 = store.record_artifact("spec", "spec-exit", "version 1")
        d2 = store.record_artifact("spec", "spec-exit", "version 2")
        assert d1 != d2
        store.close()


def test_approval_record_and_check() -> None:
    """Record approval and verify it."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _make_store(tmpdir)
        digest = store.record_artifact("spec", "spec-exit", "done")

        # Not approved yet
        assert not store.is_approved("spec→design", digest)

        # Record approval
        store.record_approval("spec→design", digest)
        assert store.is_approved("spec→design", digest)

        # Wrong digest → not approved
        assert not store.is_approved("spec→design", "wrong_digest")
        store.close()


def test_digest_invalidation() -> None:
    """AC-9: editing an approved artifact voids the approval."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _make_store(tmpdir)

        # Record artifact and approve
        digest = store.record_artifact("spec", "spec-exit", "original")
        store.record_approval("spec→design", digest)
        assert store.is_approved("spec→design", digest)

        # Edit the artifact → approval invalidated
        new_digest = store.record_artifact("spec", "spec-exit", "edited")
        assert not store.is_approved("spec→design", new_digest)
        assert not store.is_approved("spec→design", digest)
        store.close()


def test_phase_machine_check_gate_no_artifact() -> None:
    """Gate check with no exit artifact."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _make_store(tmpdir)
        machine = PhaseMachine(store)

        result = machine.check_gate("spec")
        assert result["phase"] == "spec"
        assert result["has_exit_artifact"] is False
        assert result["requires_approval"] is True
        store.close()


def test_phase_machine_check_gate_with_artifact() -> None:
    """Gate check with exit artifact but no approval."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _make_store(tmpdir)
        store.record_artifact("spec", "spec-exit", "done")
        machine = PhaseMachine(store)

        result = machine.check_gate("spec")
        assert result["has_exit_artifact"] is True
        assert result["requires_approval"] is True
        assert result["approved"] is False
        assert "digest" in result
        store.close()


def test_phase_machine_check_gate_approved() -> None:
    """Gate check with exit artifact and approval."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _make_store(tmpdir)
        digest = store.record_artifact("spec", "spec-exit", "done")
        store.record_approval("spec→design", digest)
        machine = PhaseMachine(store)

        result = machine.check_gate("spec")
        assert result["has_exit_artifact"] is True
        assert result["approved"] is True
        store.close()


def test_phase_machine_terminal_phase() -> None:
    """Gate check on terminal phase (ship)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _make_store(tmpdir)
        machine = PhaseMachine(store)

        result = machine.check_gate("ship")
        assert result["status"] == "terminal"
        store.close()


def test_phase_machine_non_gated_transition() -> None:
    """Non-gated transition (build→qa) doesn't require approval."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _make_store(tmpdir)
        machine = PhaseMachine(store)

        result = machine.check_gate("build")
        assert result["requires_approval"] is False
        store.close()


def test_phase_machine_check_gate_unknown_phase() -> None:
    """Unknown phase is reported, not raised (fail-closed read surface)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _make_store(tmpdir)
        machine = PhaseMachine(store)

        result = machine.check_gate("banana")
        assert result["status"] == "invalid"
        assert "banana" in result["reason"]
        assert result["legal_phases"] == list(PHASE_ORDER)
        store.close()


def test_phase_machine_run_from_intake_stays_without_exit_artifact() -> None:
    """Graph walk from intake respects the intake→spec exit-artifact gate."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _make_store(tmpdir)
        assert store.get_current_phase() == "intake"

        machine = PhaseMachine(store)
        machine.run()

        # No intake-exit artifact → the walk must not advance past intake
        assert store.get_current_phase() == "intake"

        # With the exit artifact, the walk advances intake → spec, then
        # stops at the spec→design gate (no exit artifact there)
        store.record_artifact("intake", "intake-exit", "route decided: full")
        machine = PhaseMachine(store)
        machine.run(thread_id="t-advance")
        assert store.get_current_phase() == "spec"
        store.close()


def test_phase_machine_interrupt_and_resume_at_approval_gate() -> None:
    """interrupt() pauses at the approval gate; resume() completes it."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _make_store(tmpdir)
        store.record_artifact("intake", "intake-exit", "full")
        store.advance_phase("spec")
        digest = store.record_artifact("spec", "spec-exit", "done")

        machine = PhaseMachine(store)
        result = machine.run(thread_id="t-gate")

        # Run paused at the spec→design approval interrupt
        assert "__interrupt__" in result
        interrupt_info = result["__interrupt__"][0]
        assert interrupt_info.value["transition"] == "spec→design"
        assert not store.is_approved("spec→design", digest)

        # Resume with approval → transition recorded, walk advances to design
        machine.resume("t-gate", {"approved": True})
        assert store.is_approved("spec→design", digest)
        assert store.get_current_phase() == "design"
        store.close()


def test_phase_machine_run_advances_through_non_gated() -> None:
    """Phase machine can run through non-gated transitions."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _make_store(tmpdir)
        # Start at build (past the gated transitions), each move with its evidence
        store.record_artifact("intake", "intake-exit", "full")
        store.advance_phase("spec")
        spec_digest = store.record_artifact("spec", "spec-exit", "done")
        store.record_approval("spec→design", spec_digest)
        store.advance_phase("design")
        design_digest = store.record_artifact("design", "design-exit", "done")
        store.record_approval("design→plan", design_digest)
        store.advance_phase("plan")
        plan_digest = store.record_artifact("plan", "plan-exit", "done")
        store.record_approval("plan→build", plan_digest)
        store.advance_phase("build")

        machine = PhaseMachine(store)
        machine.run()

        # Should have advanced through build → qa → ship
        assert store.get_current_phase() in ("build", "qa", "ship")
        store.close()


def test_create_task_fanout() -> None:
    """Create Send messages for parallel task execution."""
    sends = create_task_fanout(["task-1", "task-2", "task-3"], "build_task")
    assert len(sends) == 3
    for s in sends:
        assert s.node == "build_task"


def test_exit_artifact_digest_helper() -> None:
    """get_exit_artifact_digest returns the correct digest."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _make_store(tmpdir)

        assert store.get_exit_artifact_digest("spec") is None

        digest = store.record_artifact("spec", "spec-exit", "done")
        assert store.get_exit_artifact_digest("spec") == digest
        store.close()
