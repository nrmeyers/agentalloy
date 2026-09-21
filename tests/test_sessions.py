"""Session management tests (AC-11: crash-resumable sessions)."""

import tempfile
from pathlib import Path

from agentalloy.sessions import SessionManager
from agentalloy.state_store import StateStore


def _make_store(tmpdir: str) -> StateStore:
    return StateStore(str(Path(tmpdir) / "state.duck"))


def test_create_session() -> None:
    """Create a new session."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _make_store(tmpdir)
        mgr = SessionManager(store)

        info = mgr.create("test-session")
        assert info.session_key == "test-session"
        assert info.status == "active"
        assert info.phase == "intake"
        store.close()


def test_list_sessions() -> None:
    """List all sessions."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _make_store(tmpdir)
        mgr = SessionManager(store)

        mgr.create("session-1")
        mgr.create("session-2")

        sessions = mgr.list_sessions()
        assert len(sessions) == 2
        keys = {s.session_key for s in sessions}
        assert keys == {"session-1", "session-2"}
        store.close()


def test_stash_and_resume() -> None:
    """AC-11: stash → resume preserves state."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _make_store(tmpdir)
        mgr = SessionManager(store)

        # Create session and add some state
        mgr.create("my-session")
        store.add_contract("contract-1", ["python"], "auth module")
        store.record_artifact("intake", "intake-exit", "full")
        store.advance_phase("spec")
        digest = store.record_artifact("spec", "spec-exit", "spec done")
        store.record_approval("spec→design", digest)
        store.advance_phase("design")

        # Stash
        snapshot = mgr.stash("my-session")
        assert snapshot is not None
        assert snapshot["phase"] == "design"
        assert len(snapshot["contracts"]) == 1
        assert len(snapshot["approvals"]) == 1

        # Verify session is stashed
        info = mgr.get("my-session")
        assert info is not None
        assert info.status == "stashed"

        # Simulate process kill — create new store from same DB
        store.close()
        store2 = _make_store(tmpdir)
        mgr2 = SessionManager(store2)

        # Resume
        result = mgr2.resume("my-session")
        assert result is not None
        assert result["phase"] == "design"

        # Verify state was restored
        assert store2.get_current_phase() == "design"
        contract = store2.get_contract("contract-1")
        assert contract is not None
        assert contract["slug"] == "contract-1"

        # Verify session is active again
        info2 = mgr2.get("my-session")
        assert info2 is not None
        assert info2.status == "active"

        store2.close()


def test_stash_nonexistent_session() -> None:
    """Stashing a nonexistent session returns None."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _make_store(tmpdir)
        mgr = SessionManager(store)

        result = mgr.stash("nonexistent")
        assert result is None
        store.close()


def test_resume_non_stashed_session() -> None:
    """Resuming a non-stashed session returns None."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _make_store(tmpdir)
        mgr = SessionManager(store)

        mgr.create("active-session")
        result = mgr.resume("active-session")
        assert result is None
        store.close()


def test_archive_session() -> None:
    """Archive a completed session."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _make_store(tmpdir)
        mgr = SessionManager(store)

        mgr.create("done-session")
        assert mgr.archive("done-session") is True

        info = mgr.get("done-session")
        assert info is not None
        assert info.status == "archived"
        store.close()


def test_cancel_session() -> None:
    """Cancel a session."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _make_store(tmpdir)
        mgr = SessionManager(store)

        mgr.create("cancelled-session")
        assert mgr.cancel("cancelled-session") is True

        info = mgr.get("cancelled-session")
        assert info is not None
        assert info.status == "cancelled"
        store.close()


def test_cancel_already_cancelled() -> None:
    """Cancelling an already-cancelled session returns False."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _make_store(tmpdir)
        mgr = SessionManager(store)

        mgr.create("session")
        mgr.cancel("session")
        assert mgr.cancel("session") is False
        store.close()


def test_work_item_cursor() -> None:
    """Work-item cursor tracks tasks through a session."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _make_store(tmpdir)
        mgr = SessionManager(store)

        mgr.create("build-session")

        # Add work items
        id1 = mgr.add_work_item("task-auth", "build-session", "contract-auth")
        id2 = mgr.add_work_item("task-api", "build-session", "contract-api")
        id3 = mgr.add_work_item("task-tests", "build-session")

        assert id1 > 0
        assert id2 > id1
        assert id3 > id2

        # Check cursor
        cursor = mgr.get_cursor("build-session")
        assert len(cursor) == 3
        assert cursor[0]["task_slug"] == "task-auth"
        assert cursor[0]["status"] == "pending"

        # Advance cursor
        mgr.advance_cursor(id1, "done")
        mgr.advance_cursor(id2, "in_progress")

        cursor2 = mgr.get_cursor("build-session")
        assert cursor2[0]["status"] == "done"
        assert cursor2[1]["status"] == "in_progress"
        assert cursor2[2]["status"] == "pending"

        store.close()


def test_session_status_includes_work_items() -> None:
    """Session status includes work-item cursor."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _make_store(tmpdir)
        mgr = SessionManager(store)

        mgr.create("session")
        mgr.add_work_item("task-1", "session")
        mgr.add_work_item("task-2", "session")

        status = mgr.status("session")
        assert status["session_key"] == "session"
        assert len(status["work_items"]) == 2

        store.close()


def test_session_status_nonexistent() -> None:
    """Status of nonexistent session returns error."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _make_store(tmpdir)
        mgr = SessionManager(store)

        status = mgr.status("nonexistent")
        assert "error" in status
        store.close()


def test_stash_preserves_work_items() -> None:
    """AC-11: stash captures work-item cursor state."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _make_store(tmpdir)
        mgr = SessionManager(store)

        mgr.create("session")
        mgr.add_work_item("task-1", "session")
        mgr.add_work_item("task-2", "session")
        mgr.advance_cursor(1, "done")

        snapshot = mgr.stash("session")
        assert snapshot is not None

        # Resume and check cursor was preserved
        result = mgr.resume("session")
        assert result is not None
        cursor = result.get("cursor", {})
        items = cursor.get("items", [])
        assert len(items) == 2
        assert items[0]["status"] == "done"

        store.close()
