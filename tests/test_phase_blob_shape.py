"""Task 01 `phase-blob-shape` — the store owns the phase blob and its semantics.

`.agentalloy/phase` carried seven fields and a set of preservation rules that
lived in `signals.skill_loader._write_phase_atomic`. Those rules move to the
store seam here, so that when the file is deleted (task 08) nothing is lost and
no caller outside `storage/` parses the blob.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentalloy.storage.state_store import DuckDBStateStore, PhaseState


@pytest.fixture()
def store(tmp_path: Path) -> DuckDBStateStore:
    s = DuckDBStateStore(tmp_path / "state.db")
    s.open()
    s.migrate()
    return s


class TestRoundTrip:
    def test_all_fields_survive(self, store: DuckDBStateStore) -> None:
        store.write_phase(
            "design", actor="sess-1", mode="free", paused_since="2026-07-28T00:00:00Z"
        )
        got = store.read_phase()
        assert got is not None
        assert got.phase == "design"
        assert got.mode == "free"
        assert got.paused_since == "2026-07-28T00:00:00Z"
        assert got.transitioned_by == "sess-1"
        assert got.started_at
        assert got.last_updated
        assert got.workflow == "sdd-design"

    def test_unset_reads_none(self, store: DuckDBStateStore) -> None:
        assert store.read_phase() is None

    def test_bare_string_row_normalizes(self, store: DuckDBStateStore) -> None:
        """A pre-blob row (or one left by ``import_from_files``) is not an error.

        Task 03 rewrites the import path; until then a bare value must read as a
        phase rather than crashing every reader.
        """
        store.write("phase", "design")
        got = store.read_phase()
        assert got == PhaseState(phase="design", workflow="sdd-design")


class TestPreservation:
    def test_free_flow_survives_a_transition(self, store: DuckDBStateStore) -> None:
        """AC-3. The regression `flow.py`'s hardcoded workflow currently masks."""
        store.write_phase("spec", mode="free", paused_since="2026-07-28T01:02:03Z")
        store.write_phase("design")  # a transition that says nothing about flow
        got = store.read_phase()
        assert got is not None
        assert got.mode == "free"
        assert got.paused_since == "2026-07-28T01:02:03Z"

    def test_empty_string_clears(self, store: DuckDBStateStore) -> None:
        """`flow resume` drops free-flow; ``None`` means "leave it", ``""`` clears."""
        store.write_phase("design", mode="free", paused_since="2026-07-28T01:02:03Z")
        store.write_phase("design", mode="", paused_since="")
        got = store.read_phase()
        assert got is not None
        assert got.mode is None
        assert got.paused_since is None

    def test_started_at_is_stable(self, store: DuckDBStateStore) -> None:
        store.write_phase("spec")
        first = store.read_phase()
        store.write_phase("design")
        second = store.read_phase()
        assert first is not None and second is not None
        assert second.started_at == first.started_at


class TestTransitionedBy:
    def test_same_phase_write_preserves_actor(self, store: DuckDBStateStore) -> None:
        """The guard behind the phase-swept confirm directive.

        A second session rewriting the *same* phase must not be able to claim it
        moved the phase — otherwise the session that really did the transition
        stops being identifiable and the "swept" directive misfires.
        """
        store.write_phase("design", actor="sess-1")
        store.write_phase("design", actor="sess-2")
        got = store.read_phase()
        assert got is not None
        assert got.transitioned_by == "sess-1"

    def test_real_transition_updates_actor(self, store: DuckDBStateStore) -> None:
        store.write_phase("design", actor="sess-1")
        store.write_phase("build", actor="sess-2")
        got = store.read_phase()
        assert got is not None
        assert got.transitioned_by == "sess-2"


class TestDerivedWorkflow:
    def test_caller_cannot_poison_workflow(self, store: DuckDBStateStore) -> None:
        """`workflow` is derived at the seam, so there is no caller-supplied path.

        Asserted structurally: the signature takes no ``workflow``, and a row
        hand-written with a bogus one still reads back derived.
        """
        import inspect

        assert "workflow" not in inspect.signature(store.write_phase).parameters

        store.write("phase", json.dumps({"phase": "design", "workflow": "sdd-EVIL"}))
        got = store.read_phase()
        assert got is not None
        assert got.workflow == "sdd-design"


class TestAtomicity:
    def test_failed_write_leaves_prior_blob_intact(self, store: DuckDBStateStore) -> None:
        """The read-modify-write is transactional: a mid-write failure rolls back.

        Without the surrounding transaction the row would already carry the new
        phase when the failure hit, leaving a blob nobody wrote deliberately.
        """
        store.write_phase("design", actor="sess-1", mode="free")

        original_write = store.write

        def write_then_boom(*args: object, **kwargs: object) -> None:
            # Fail AFTER the row is written, not instead of it — otherwise the
            # prior blob survives trivially and the test proves nothing about
            # the transaction.
            original_write(*args, **kwargs)  # type: ignore[arg-type]
            raise RuntimeError("disk went away")

        store.write = write_then_boom  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="disk went away"):
            store.write_phase("build", actor="sess-2")
        store.write = original_write  # type: ignore[method-assign]

        got = store.read_phase()
        assert got is not None
        assert got.phase == "design"
        assert got.transitioned_by == "sess-1"
        assert got.mode == "free"
