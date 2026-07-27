"""Integration tests for the SDD state store (DuckDBStateStore).

Covers schema lifecycle, repo/session-scoped read/write, lease management,
file-mirror round-trip, and import-from-files — all against a real DuckDB
database in a temporary directory.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from agentalloy.storage.state_store import (
    DuckDBStateStore,
    LeaseConflict,
    open_state_store,
)

# ---------------------------------------------------------------------------
# Transaction seam (TA4)
# ---------------------------------------------------------------------------


class TestTransaction:
    """Transaction context manager: BEGIN/COMMIT, ROLLBACK on exception."""

    def test_commit_on_success(self, tmp_path: Path) -> None:
        """Writes inside transaction() are visible after COMMIT."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            with store.transaction() as tx:
                tx.write("phase", "build")
            # After commit, the value is visible.
            assert store.read("phase") == "build"

    def test_rollback_on_exception_leaves_db_unchanged(self, tmp_path: Path) -> None:
        """TA4 — an exception inside the block rolls back all writes."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            # Seed a known phase value.
            store.write("phase", "spec")
            original_phase = store.read("phase")

            with pytest.raises(ValueError, match="boom"):
                with store.transaction() as tx:
                    tx.write("phase", "build")
                    tx.write("cursor", "task-99")
                    raise ValueError("boom")

            # Phase should be unchanged — rollback undid the write.
            assert store.read("phase") == original_phase
            # The cursor write should also be rolled back.
            assert store.read("cursor") is None

    def test_rollback_preserves_byte_identity(self, tmp_path: Path) -> None:
        """An exception leaves the database file byte-identical to pre-block state."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            store.write("phase", "spec")
            pre_bytes = db.read_bytes()

            with pytest.raises(RuntimeError, match="fail"):
                with store.transaction() as tx:
                    tx.write("phase", "build")
                    tx.write("cursor", "task-99")
                    raise RuntimeError("fail")

            # The DB file should be byte-identical (DuckDB rollback is in-place).
            assert db.read_bytes() == pre_bytes
            assert store.read("phase") == "spec"
            assert store.read("cursor") is None

    def test_exception_is_re_raised(self, tmp_path: Path) -> None:
        """The original exception propagates after ROLLBACK."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            with pytest.raises(ValueError, match="original"):
                with store.transaction():
                    raise ValueError("original")

    def test_reentrant_call_raises(self, tmp_path: Path) -> None:
        """A nested transaction() call raises RuntimeError."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            with store.transaction():
                with pytest.raises(RuntimeError, match="nested"):
                    with store.transaction():
                        pass

    def test_readonly_refuses_transaction(self, tmp_path: Path) -> None:
        """A read-only store refuses to begin a transaction."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as writer:
            writer.migrate()
        store = DuckDBStateStore(db, read_only=True)
        with store:
            with pytest.raises(RuntimeError, match="read-only"):
                with store.transaction():
                    pass

    def test_execute_works_inside_transaction(self, tmp_path: Path) -> None:
        """execute() works inside a transaction block."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            with store.transaction() as tx:
                tx.execute(
                    "INSERT INTO sdd_state (repo, kind, session_key, value, updated_at) "
                    "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
                    (store._repo(), "phase", None, "build"),
                )
            assert store.read("phase") == "build"

    def test_acquire_lease_works_inside_transaction(self, tmp_path: Path) -> None:
        """acquire_lease() works inside a transaction block."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            with store.transaction() as tx:
                result = tx.acquire_lease("phase", "s1")
                assert result.acquired is True
            # Lease should persist after commit.
            result2 = store.acquire_lease("phase", "s2")
            assert result2.acquired is False  # s1 still holds it

    def test_release_lease_works_inside_transaction(self, tmp_path: Path) -> None:
        """release_lease() works inside a transaction block."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            store.acquire_lease("phase", "s1")
            with store.transaction() as tx:
                tx.release_lease("phase", "s1")
            # Lease should be released after commit.
            result = store.acquire_lease("phase", "s2")
            assert result.acquired is True

    def test_rollback_undoes_lease_acquisition(self, tmp_path: Path) -> None:
        """A rolled-back lease acquisition does not persist."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            with pytest.raises(ValueError):
                with store.transaction() as tx:
                    tx.acquire_lease("phase", "s1")
                    raise ValueError("abort")
            # s1's lease should not exist; s2 can acquire freely.
            result = store.acquire_lease("phase", "s2")
            assert result.acquired is True


# ---------------------------------------------------------------------------
# Schema lifecycle
# ---------------------------------------------------------------------------


class TestStoreSchema:
    """Schema creation / migration behaviour."""

    def test_create_and_migrate(self, tmp_path: Path) -> None:
        """A fresh DB gets the sdd_state table on open+migrate."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            assert db.exists()
            # Verify the table exists by querying it.
            row = store.scalar("SELECT COUNT(*) FROM sdd_state")
            assert row == 0

    def test_migrate_idempotent(self, tmp_path: Path) -> None:
        """Calling migrate() twice does not raise."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            store.migrate()  # Should not raise

    def test_open_read_only_cannot_migrate(self, tmp_path: Path) -> None:
        """Read-only stores refuse migrate."""
        db = tmp_path / "test.duck"
        # Create the DB first (read-only can't open a non-existent DB).
        with DuckDBStateStore(db).open() as writer:
            writer.migrate()
        # Now open read-only and verify migrate is refused.
        store = DuckDBStateStore(db, read_only=True)
        with store:
            with pytest.raises(RuntimeError, match="cannot migrate"):
                store.migrate()

    def test_open_read_only_cannot_write(self, tmp_path: Path) -> None:
        """Read-only stores refuse write."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as writer:
            writer.migrate()
        store = DuckDBStateStore(db, read_only=True)
        with store:
            with pytest.raises(RuntimeError, match="cannot write"):
                store.write("phase", "spec")

    def test_open_read_only_cannot_acquire_lease(self, tmp_path: Path) -> None:
        """Read-only stores refuse lease acquisition."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as writer:
            writer.migrate()
        store = DuckDBStateStore(db, read_only=True)
        with store:
            with pytest.raises(RuntimeError, match="cannot acquire lease"):
                store.acquire_lease("phase", "s1")

    def test_open_read_only_cannot_release_lease(self, tmp_path: Path) -> None:
        """Read-only stores refuse lease release."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as writer:
            writer.migrate()
        store = DuckDBStateStore(db, read_only=True)
        with store:
            with pytest.raises(RuntimeError, match="cannot release"):
                store.release_lease("phase", "s1")

    def test_unopen_conn_raises(self) -> None:
        """Accessing conn before open raises RuntimeError."""
        store = DuckDBStateStore(Path("/tmp/test.duck"))
        with pytest.raises(RuntimeError, match="not open"):
            _ = store.conn


# ---------------------------------------------------------------------------
# Read / Write
# ---------------------------------------------------------------------------


class TestReadWrite:
    """Repo-scoped and session-scoped read/write."""

    def test_repo_scoped_write_and_read(self, tmp_path: Path) -> None:
        """Repo-scoped kinds (phase) are visible with session_key=None."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            store.write("phase", "spec", owner="s1")
            assert store.read("phase") == "spec"

    def test_repo_scoped_overwrite(self, tmp_path: Path) -> None:
        """Subsequent writes overwrite the previous value."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            store.write("phase", "intake")
            store.write("phase", "spec")
            assert store.read("phase") == "spec"

    def test_session_scoped_isolation(self, tmp_path: Path) -> None:
        """Different session keys see different values for the same kind."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            store.write("announced", "v1", session_key="s1")
            store.write("announced", "v2", session_key="s2")
            assert store.read("announced", session_key="s1") == "v1"
            assert store.read("announced", session_key="s2") == "v2"

    def test_read_missing_returns_none(self, tmp_path: Path) -> None:
        """Reading a non-existent kind returns None."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            assert store.read("nonexistent") is None

    def test_write_returns_result(self, tmp_path: Path) -> None:
        """write() returns a StateWriteResult with expected fields."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            result = store.write("phase", "spec", owner="s1")
            assert result.success is True
            assert result.kind == "phase"
            assert result.value == "spec"
            assert result.owner == "s1"

    def test_write_with_no_owner(self, tmp_path: Path) -> None:
        """Writes without an owner still succeed."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            result = store.write("cursor", "task-1")
            assert result.success is True
            assert result.owner is None
            assert store.read("cursor") == "task-1"


# ---------------------------------------------------------------------------
# Lease management
# ---------------------------------------------------------------------------


class TestLease:
    """Lease acquisition, conflict, and release."""

    def test_acquire_on_new_row(self, tmp_path: Path) -> None:
        """Acquiring a lease on a non-existent row succeeds."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            result = store.acquire_lease("phase", "s1")
            assert result.acquired is True
            assert result.owner == "s1"
            assert result.conflict is None

    def test_acquire_and_release(self, tmp_path: Path) -> None:
        """A lease can be acquired and then released."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            result = store.acquire_lease("phase", "s1")
            assert result.acquired is True
            store.release_lease("phase", "s1")
            # After release, another session can acquire.
            result2 = store.acquire_lease("phase", "s2")
            assert result2.acquired is True
            assert result2.owner == "s2"

    def test_conflict_with_active_lease(self, tmp_path: Path) -> None:
        """A second session cannot acquire a lease held by another."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            store.acquire_lease("phase", "s1")
            result = store.acquire_lease("phase", "s2")
            assert result.acquired is False
            assert isinstance(result.conflict, LeaseConflict)
            assert result.conflict.owner == "s1"

    def test_acquire_on_non_leased_kind_raises(self, tmp_path: Path) -> None:
        """Acquiring a lease on a non-leased kind raises ValueError."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            with pytest.raises(ValueError, match="cannot acquire lease on non-leased kind"):
                store.acquire_lease("cursor", "s1")

    def test_release_nonexistent_lease_succeeds(self, tmp_path: Path) -> None:
        """Releasing a lease that was never acquired is a no-op (no error)."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            store.release_lease("phase", "s1")  # No error

    def test_lease_expiry_allows_takeover(self, tmp_path: Path) -> None:
        """A lease that has expired can be taken over by another session."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            # Acquire with a very short duration.
            store.acquire_lease("phase", "s1", duration=timedelta(microseconds=1))
            # Wait for expiry.
            import time

            time.sleep(0.05)
            # Now s2 should be able to take over.
            result = store.acquire_lease("phase", "s2")
            assert result.acquired is True
            assert result.owner == "s2"

    def test_lease_refresh(self, tmp_path: Path) -> None:
        """The owning session can refresh its lease."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            store.acquire_lease("phase", "s1")
            result = store.acquire_lease("phase", "s1")
            assert result.acquired is True
            assert result.owner == "s1"


# ---------------------------------------------------------------------------
# File mirror
# ---------------------------------------------------------------------------


class TestFileMirror:
    """File-mirror import and export."""

    def test_mirror_to_files_creates_file(self, tmp_path: Path) -> None:
        """mirror_to_files writes a .agentalloy/<kind> file."""
        ag_dir = tmp_path / ".agentalloy"
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            ok = store.mirror_to_files("phase", "spec", ag_dir)
            assert ok is True
            phase_file = ag_dir / "phase"
            assert phase_file.exists()
            assert phase_file.read_text().strip() == "spec"

    def test_mirror_to_approved_creates_phase_file(self, tmp_path: Path) -> None:
        """approved kind writes to .agentalloy/approved/<phase>."""
        ag_dir = tmp_path / ".agentalloy"
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            ok = store.mirror_to_files("approved", "spec", ag_dir)
            assert ok is True
            approved_file = ag_dir / "approved" / "spec"
            assert approved_file.exists()

    def test_mirror_to_files_fails_gracefully(self, tmp_path: Path) -> None:
        """mirror_to_files returns False on OS error."""
        db = tmp_path / "test.duck"
        # Pass a non-existent, non-writable path.
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            ok = store.mirror_to_files("phase", "spec", Path("/nonexistent/deeply/absent"))
            assert ok is False

    def test_import_from_files_skips_existing(self, tmp_path: Path) -> None:
        """import_from_files skips kinds already in the store."""
        ag_dir = tmp_path / ".agentalloy"
        ag_dir.mkdir(parents=True, exist_ok=True)
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            # Pre-seed phase in the store.
            store.write("phase", "spec")
            # Write cursor to the file mirror.
            (ag_dir / "cursor").write_text("task-1\n")
            imported = store.import_from_files(ag_dir)
            # phase should NOT be imported (already in store).
            assert "phase" not in imported
            # cursor SHOULD be imported.
            assert "cursor" in imported
            assert store.read("cursor") == "task-1"

    def test_import_from_files_missing_dir(self, tmp_path: Path) -> None:
        """import_from_files handles missing agentalloy dir gracefully."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            imported = store.import_from_files(tmp_path / "nonexistent")
            assert imported == {}

    def test_round_trip_write_mirror_read(self, tmp_path: Path) -> None:
        """Write to store → mirror to files → read from file mirror."""
        ag_dir = tmp_path / ".agentalloy"
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            store.write("phase", "design")
            store.mirror_to_files("phase", "design", ag_dir)
            # The file should reflect the store value.
            assert (ag_dir / "phase").read_text().strip() == "design"


# ---------------------------------------------------------------------------
# open_state_store convenience
# ---------------------------------------------------------------------------


class TestOpenStateStore:
    """The open_state_store() helper."""

    def test_open_auto_migrates(self, tmp_path: Path) -> None:
        """open_state_store opens and migrates in one call."""
        db = tmp_path / "test.duck"
        store = open_state_store(db)
        try:
            row = store.scalar("SELECT COUNT(*) FROM sdd_state")
            assert row == 0
        finally:
            store.close()

    def test_open_read_only_no_migrate(self, tmp_path: Path) -> None:
        """open_state_store with read_only=True does not migrate."""
        db = tmp_path / "test.duck"
        # DuckDB refuses to open a non-existent DB in read-only mode, so create
        # the DB first (writer mode), then open read-only.
        writer = open_state_store(db)
        writer.close()
        store = open_state_store(db, read_only=True)
        try:
            # Read-only should not raise on open; migrate is skipped.
            row = store.scalar("SELECT COUNT(*) FROM sdd_state")
            assert row == 0
        finally:
            store.close()
