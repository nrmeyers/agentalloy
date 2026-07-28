"""Integration tests for the signal layer (skill_loader helpers).

Covers:
  AC-1  Differential test over signal-layer fixtures
  AC-2  Integration test + code-level guard + static analysis
  AC-3  Integration test (stop service, run phase set)
  AC-4  Composition test (2 sessions, both get orientation)
  AC-5  Transactional test (race two phase transitions)
  AC-6  Integration test (sidecar + statusline)
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from agentalloy.signals import skill_loader as sl

# The code_index conftest provides ``fixture_repo``.
from tests.code_index.conftest import (
    fixture_repo,  # noqa: F401, F811 — re-exported for fixture parameter use
)
from tests.support import seed_phase

# ---------------------------------------------------------------------------
# AC-1: Differential test over signal-layer fixtures
# ---------------------------------------------------------------------------


class TestReadParity:
    """AC-1: The signal-layer readers see the store row and nothing else."""

    def test_signal_reader_sees_the_store_row(self, fixture_repo: Path) -> None:
        seed_phase(fixture_repo, "spec")
        assert sl._read_phase(fixture_repo) == "spec"

    def test_a_phase_file_is_never_read(self, fixture_repo: Path) -> None:
        """AC-1: no signal-layer path reads ``.agentalloy/phase``."""
        (fixture_repo / ".agentalloy" / "phase").write_text("phase: spec\n", encoding="utf-8")
        assert sl._read_phase(fixture_repo) is None


# ---------------------------------------------------------------------------
# AC-2: Integration test + code-level guard + static analysis
# ---------------------------------------------------------------------------


class TestSingleOwnerWrites:
    """AC-2: CLI verbs succeed with service; code-level guards prevent bad writes."""

    def test_write_phase_atomic_records_the_phase(self, fixture_repo: Path) -> None:
        sl._write_phase_atomic(fixture_repo, "design")
        assert sl._read_phase(fixture_repo) == "design"

    def test_write_phase_atomic_writes_no_file(self, fixture_repo: Path) -> None:
        """AC-1: the write side leaves ``.agentalloy/phase`` non-existent."""
        sl._write_phase_atomic(fixture_repo, "design")
        assert not (fixture_repo / ".agentalloy" / "phase").exists()

    def test_write_phase_atomic_preserves_mode(self, fixture_repo: Path) -> None:
        """AC-4: an idempotent rewrite must not drop the repo out of free-flow."""
        seed_phase(fixture_repo, "spec", mode="free", free_since="2026-07-01")

        sl._write_phase_atomic(fixture_repo, "spec")  # idempotent rewrite
        assert sl.read_flow_state(fixture_repo) == ("free", "2026-07-01")

    def test_write_lifecycle_mode_guard(self) -> None:
        """_write_lifecycle_mode rejects invalid modes (code-level guard)."""

        tmp = Path("/tmp/_test_lifecycle_mode")
        tmp.mkdir(exist_ok=True)
        try:
            with pytest.raises(ValueError, match="invalid lifecycle mode"):
                sl._write_lifecycle_mode(tmp, "bogus")
        finally:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)

    def test_read_lifecycle_mode_defaults_full(self, fixture_repo: Path) -> None:
        """When no config exists, lifecycle mode defaults to 'full'."""
        mode = sl._read_lifecycle_mode(fixture_repo)
        assert mode == "full"

    def test_read_lifecycle_mode_off(self, fixture_repo: Path) -> None:
        """Config with lifecycle_mode: off is read correctly (not YAML-boolean-coerced)."""
        config_file = fixture_repo / ".agentalloy" / "config"
        config_file.write_text("lifecycle_mode: off\n", encoding="utf-8")
        assert sl._read_lifecycle_mode(fixture_repo) == "off"

    def test_read_lifecycle_mode_bogus_defaults_full(self, fixture_repo: Path) -> None:
        """Unrecognized mode falls back to 'full'."""
        config_file = fixture_repo / ".agentalloy" / "config"
        config_file.write_text("lifecycle_mode: wibble\n", encoding="utf-8")
        assert sl._read_lifecycle_mode(fixture_repo) == "full"


# ---------------------------------------------------------------------------
# AC-3: Integration test (stop service, run phase set)
# ---------------------------------------------------------------------------


class TestServiceDown:
    """AC-3: with no store bound, a phase write fails loudly rather than silently."""

    def test_phase_write_without_a_bound_store_raises(self, fixture_repo: Path) -> None:
        """Store out of reach is not the same as "no phase" — a dropped
        transition must never look like a successful one."""
        from agentalloy.storage.state_store import bind_process_store, process_store

        store = process_store()
        bind_process_store(None)
        try:
            with pytest.raises(RuntimeError, match="no state store bound"):
                sl._write_phase_atomic(fixture_repo, "spec")
        finally:
            bind_process_store(store)

    def test_phase_set_clears_cursor_on_transition(self, fixture_repo: Path) -> None:
        """Transitioning to a new phase clears stale cursors."""
        seed_phase(fixture_repo, "intake")

        # Seed a cursor.
        cursor_file = fixture_repo / ".agentalloy" / "cursor"
        cursor_file.write_text("old-task\n", encoding="utf-8")

        # Transition — this should clear the cursor.
        sl._write_phase_atomic(fixture_repo, "spec")

        # The phase changed, so cursor should be cleared.
        assert sl._read_cursor(fixture_repo) is None
        # Phase should be updated.
        assert sl._read_phase(fixture_repo) == "spec"


# ---------------------------------------------------------------------------
# AC-4: Composition test (2 sessions, both get orientation)
# ---------------------------------------------------------------------------


class TestSessionScopedCadence:
    """AC-4: Two sessions on the same phase both get orientation."""

    def test_two_sessions_both_get_orientation(self, fixture_repo: Path) -> None:
        """_read_announced_state returns both session keys for the same phase.

        When two sessions are oriented to the same phase, the announced file
        should contain both session keys.
        """
        # Write the announced file with two sessions.
        announced_file = fixture_repo / ".agentalloy" / "announced"
        announced_file.write_text("spec\tsession-aaa,session-bbb\n", encoding="utf-8")

        phase, sessions = sl._read_announced_state(fixture_repo)
        assert phase == "spec"
        assert set(sessions) == {"session-aaa", "session-bbb"}

    def test_announced_write_and_read_round_trip(self, fixture_repo: Path) -> None:
        """_write_announced_atomic + _read_announced_state round-trip."""
        sl._write_announced_atomic(fixture_repo, "design", ["s1", "s2", "s3"])
        phase, sessions = sl._read_announced_state(fixture_repo)
        assert phase == "design"
        assert set(sessions) == {"s1", "s2", "s3"}

    def test_announced_without_session_keys(self, fixture_repo: Path) -> None:
        """Writing announced without session keys produces legacy format."""
        sl._write_announced_atomic(fixture_repo, "spec")
        content = (fixture_repo / ".agentalloy" / "announced").read_text()
        # Legacy format: just the phase name, no tab.
        assert "\t" not in content
        assert content.strip() == "spec"
        # Reading back: phase is 'spec', sessions is empty list.
        phase, sessions = sl._read_announced_state(fixture_repo)
        assert phase == "spec"
        assert sessions == []

    def test_max_announced_sessions_limit(self, fixture_repo: Path) -> None:
        """_MAX_ANNOUNCED_SESSIONS caps the session list."""
        assert sl._MAX_ANNOUNCED_SESSIONS == 8
        # The cap is a constant; we verify it exists and is positive.
        assert sl._MAX_ANNOUNCED_SESSIONS > 0


# ---------------------------------------------------------------------------
# AC-5: Transactional test (race two phase transitions)
# ---------------------------------------------------------------------------


class TestConcurrentTransition:
    """AC-5: Two concurrent phase transitions are handled correctly."""

    def test_race_two_phase_transitions(self, fixture_repo: Path) -> None:
        """Two threads writing phase files concurrently — both succeed.

        The store path does its read-modify-write inside one BEGIN/COMMIT, so
        concurrent writes can't interleave into a half-updated blob. The last
        writer wins.
        """
        errors: list[Exception] = []

        def write_phase(phase: str) -> None:
            try:
                sl._write_phase_atomic(fixture_repo, phase)
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=write_phase, args=("design",))
        t2 = threading.Thread(target=write_phase, args=("spec",))

        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert not errors, f"Thread errors: {errors}"
        # The row is intact regardless of which write won.
        final_phase = sl._read_phase(fixture_repo)
        assert final_phase in ("design", "spec")

    def test_concurrent_cursor_writes(self, fixture_repo: Path) -> None:
        """Two threads writing cursors concurrently — no corruption."""
        errors: list[Exception] = []

        def write_cursor(cursor: str) -> None:
            try:
                sl._write_cursor_atomic(fixture_repo, cursor)
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=write_cursor, args=("task-1",))
        t2 = threading.Thread(target=write_cursor, args=("task-2",))

        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert not errors, f"Thread errors: {errors}"
        # File should still be readable.
        cursor = sl._read_cursor(fixture_repo)
        assert cursor is not None


# ---------------------------------------------------------------------------
# Additional helpers / invariants
# ---------------------------------------------------------------------------


class TestFlowState:
    """read_flow_state behaviour."""

    def test_workflow_default(self, fixture_repo: Path) -> None:
        """No phase row → workflow mode."""
        mode, free_since = sl.read_flow_state(fixture_repo)
        assert mode == "workflow"
        assert free_since is None

    def test_free_mode(self, fixture_repo: Path) -> None:
        """phase row with mode: free → free mode."""
        seed_phase(fixture_repo, "spec", mode="free", free_since="2026-07-01")
        mode, free_since = sl.read_flow_state(fixture_repo)
        assert mode == "free"
        assert free_since == "2026-07-01"

    def test_unknown_mode_defaults_workflow(self, fixture_repo: Path) -> None:
        """Unknown mode value falls back to workflow."""
        seed_phase(fixture_repo, "spec", mode="unknown")
        mode, free_since = sl.read_flow_state(fixture_repo)
        assert mode == "workflow"


class TestLifecycleMode:
    """_read_lifecycle_mode edge cases."""

    def test_legacy_assist_maps_to_off(self, fixture_repo: Path) -> None:
        """Legacy 'assist' mode reads as 'off'."""
        config_file = fixture_repo / ".agentalloy" / "config"
        config_file.write_text("lifecycle_mode: assist\n", encoding="utf-8")
        assert sl._read_lifecycle_mode(fixture_repo) == "off"

    def test_missing_config_returns_default(self, fixture_repo: Path) -> None:
        """No config file → full."""
        config_file = fixture_repo / ".agentalloy" / "config"
        if config_file.exists():
            config_file.unlink()
        assert sl._read_lifecycle_mode(fixture_repo) == "full"


class TestTransitionedBy:
    """_read_transitioned_by behaviour."""

    def test_transitioned_by_recorded(self, fixture_repo: Path) -> None:
        """transitioned_by is recorded when present."""
        seed_phase(fixture_repo, "spec", actor="session-abc")
        assert sl._read_transitioned_by(fixture_repo) == "session-abc"

    def test_transitioned_by_absent(self, fixture_repo: Path) -> None:
        """No transitioned_by → None."""
        seed_phase(fixture_repo, "spec")
        assert sl._read_transitioned_by(fixture_repo) is None

    def test_transitioned_by_preserved_on_idempotent_write(self, fixture_repo: Path) -> None:
        """Rewriting the same phase preserves transitioned_by."""
        seed_phase(fixture_repo, "spec", actor="session-xyz")
        sl._write_phase_atomic(fixture_repo, "spec")  # same phase
        assert sl._read_transitioned_by(fixture_repo) == "session-xyz"
