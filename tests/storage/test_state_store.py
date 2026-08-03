"""Integration tests for the SDD state store (DuckDBStateStore).

Covers schema lifecycle, repo/session-scoped read/write, lease management,
file-mirror round-trip, and import-from-files — all against a real DuckDB
database in a temporary directory.
"""

from __future__ import annotations

import threading
from datetime import timedelta
from pathlib import Path

import pytest

from agentalloy.storage.state_store import (
    DuckDBStateStore,
    LeaseConflict,
    StateStoreError,
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
            # Row must exist before leasing (lease claims ownership, does not create).
            store.write("phase", "")
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
            store.write("phase", "")
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
            store.write("phase", "")
            with pytest.raises(ValueError):
                with store.transaction() as tx:
                    tx.acquire_lease("phase", "s1")
                    raise ValueError("abort")
            # s1's lease should not exist; s2 can acquire freely.
            result = store.acquire_lease("phase", "s2")
            assert result.acquired is True

    def test_acquire_lease_on_nonexistent_row_returns_conflict(self, tmp_path: Path) -> None:
        """acquire_lease must not create rows — ghost rows violate lease semantics."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            # No write() — row does not exist.
            result = store.acquire_lease("phase", "s1")
            assert result.acquired is False
            assert result.conflict is not None
            assert "write it before leasing" in result.conflict.message


# ---------------------------------------------------------------------------
# Schema lifecycle
# ---------------------------------------------------------------------------


class TestConcurrentConnectionUse:
    """One connection, many threads — the shape the service actually runs.

    The service opens a single ``state.duck`` and serves every request from it
    on a threadpool, so reads and writes genuinely overlap.  A DuckDB
    connection carries one pending result, so handing a live cursor back to the
    caller lets a second thread's statement replace the rows before the first
    thread fetches them.  That does not raise where it happens — it surfaces
    later as a phase row that reads back as garbage.
    """

    def test_reads_racing_writes_stay_coherent(self, tmp_path: Path) -> None:
        store = DuckDBStateStore(tmp_path / "race.duck")
        store.open()
        store.migrate()
        repo = store.for_repo("repo-a")
        repo.write_phase("spec")

        errors: list[BaseException] = []
        seen: set[str] = set()
        stop = threading.Event()

        def reader() -> None:
            try:
                while not stop.is_set():
                    state = repo.read_phase()
                    if state is not None:
                        seen.add(state.phase)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def writer() -> None:
            try:
                for i in range(200):
                    repo.write_phase("build" if i % 2 else "spec")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        readers = [threading.Thread(target=reader) for _ in range(4)]
        writers = [threading.Thread(target=writer) for _ in range(3)]
        for t in (*writers, *readers):
            t.start()
        for t in writers:
            t.join()
        stop.set()
        for t in readers:
            t.join()
        store.close()

        assert not errors, f"{len(errors)} errors, first: {errors[0]!r}"
        # Never a phase nobody wrote: a torn read shows up as a bogus value,
        # not only as an exception.
        assert seen <= {"spec", "build"}

    def test_a_transaction_excludes_other_threads(self, tmp_path: Path) -> None:
        """A write from another thread lands after the transaction commits."""
        store = DuckDBStateStore(tmp_path / "txn.duck")
        store.open()
        store.migrate()
        repo = store.for_repo("repo-a")
        repo.write_phase("spec")

        inside = threading.Event()
        released = threading.Event()

        def other() -> None:
            inside.wait(timeout=5)
            repo.write_phase("qa")
            released.set()

        t = threading.Thread(target=other)
        t.start()
        with repo.transaction():
            inside.set()
            # The other thread is blocked on the connection lock, so it cannot
            # have interleaved a write into the middle of this transaction.
            assert not released.wait(timeout=0.3)
            repo.write_phase("build")
        t.join(timeout=5)

        assert repo.read_phase() is not None
        assert repo.read_phase().phase == "qa"  # pyright: ignore[reportOptionalMemberAccess]
        store.close()


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

    def test_migrate_adds_status_column(self, tmp_path: Path) -> None:
        """TC1 — ALTER TABLE adds status column to sdd_artifact."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            # Verify the column exists by querying it.
            row = store.scalar("SELECT COUNT(*) FROM sdd_artifact WHERE status = 'active'")
            assert row == 0  # table exists, no rows yet

    def test_migrate_status_column_idempotent(self, tmp_path: Path) -> None:
        """TC2 — ALTER TABLE is idempotent (no error on second migrate)."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            store.migrate()  # Should not raise
            # Column should still work.
            row = store.scalar("SELECT COUNT(*) FROM sdd_artifact WHERE status = 'active'")
            assert row == 0


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

    def test_write_leased_kind_before_row_exists(self, tmp_path: Path) -> None:
        """First write to a leased kind (phase/approved) must not block.

        When no row exists yet, the inline lease check must not produce a
        conflict — the write creates the row and proceeds.
        """
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            # No seed write — the row does not exist.
            result = store.write("phase", "spec", owner="s1")
            assert result.success is True
            assert result.conflict is None
            assert result.owner == "s1"
            assert store.read("phase") == "spec"

        # Same for the other leased kind.
        db2 = tmp_path / "test2.duck"
        with DuckDBStateStore(db2).open() as store2:
            store2.migrate()
            result = store2.write("approved", "true", owner="s1")
            assert result.success is True
            assert result.conflict is None


# ---------------------------------------------------------------------------
# Lease management
# ---------------------------------------------------------------------------


class TestLease:
    """Lease acquisition, conflict, and release."""

    def _seed_phase(self, store: DuckDBStateStore) -> None:
        """Write a phase row so acquire_lease has a row to claim."""
        store.write("phase", "")

    def test_acquire_on_new_row(self, tmp_path: Path) -> None:
        """Acquiring a lease on an existing row claims ownership."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            self._seed_phase(store)
            result = store.acquire_lease("phase", "s1")
            assert result.acquired is True
            assert result.owner == "s1"
            assert result.conflict is None

    def test_acquire_and_release(self, tmp_path: Path) -> None:
        """A lease can be acquired and then released."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            self._seed_phase(store)
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
            self._seed_phase(store)
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
            self._seed_phase(store)
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
            self._seed_phase(store)
            store.acquire_lease("phase", "s1")
            result = store.acquire_lease("phase", "s1")
            assert result.acquired is True
            assert result.owner == "s1"


# ---------------------------------------------------------------------------
# File import (one-way: legacy .agentalloy files -> store)
# ---------------------------------------------------------------------------


class TestFileImport:
    """One-way import of legacy .agentalloy files into the store."""

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


# ---------------------------------------------------------------------------
# Contract schema + CRUD (TA1, TA7, TA8, TA9)
# ---------------------------------------------------------------------------


class TestContractSchema:
    """sdd_contract table creation and migration."""

    def test_contract_table_created_by_migrate(self, tmp_path: Path) -> None:
        """migrate() creates the sdd_contract table."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            count = store.scalar("SELECT COUNT(*) FROM sdd_contract")
            assert count == 0

    def test_migrate_on_existing_db_no_data_loss(self, tmp_path: Path) -> None:
        """migrate() on an existing DB preserves sdd_state data."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            store.write("phase", "build")
        # Re-open and re-migrate — simulates schema extension on existing DB
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            assert store.read("phase") == "build"
            # Contract table should also exist
            count = store.scalar("SELECT COUNT(*) FROM sdd_contract")
            assert count == 0


class TestContractCRUD:
    """TA1 — put_contract / get_contract / list_contracts."""

    def test_put_and_get_contract(self, tmp_path: Path) -> None:
        """Store a contract and retrieve it by contract_id."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            cid = store.put_contract(
                "ctr-001",
                phase="build",
                slug="my-slug",
                work_item="03-contract-schema",
                route="full",
                domain_tags=["state-management"],
                body="# Contract body",
            )
            assert cid == "ctr-001"

            contract = store.get_contract("ctr-001")
            assert contract is not None
            assert contract["contract_id"] == "ctr-001"
            assert contract["phase"] == "build"
            assert contract["slug"] == "my-slug"
            assert contract["work_item"] == "03-contract-schema"
            assert contract["route"] == "full"
            assert contract["domain_tags"] == ["state-management"]
            assert contract["body"] == "# Contract body"
            assert contract["status"] == "active"

    def test_get_missing_contract_returns_none(self, tmp_path: Path) -> None:
        """get_contract for a non-existent ID returns None."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            assert store.get_contract("nonexistent") is None

    # TA1 — a stored contract is retrievable by phase
    def test_ta1_list_contracts_by_phase(self, tmp_path: Path) -> None:
        """TA1 — stored contracts are retrievable via list_contracts(phase=)."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            store.put_contract("ctr-build-1", phase="build", slug="my-slug", body="build contract")
            store.put_contract(
                "ctr-design-1", phase="design", slug="my-slug", body="design contract"
            )
            store.put_contract(
                "ctr-build-2", phase="build", slug="other-slug", body="another build"
            )

            # Filter by phase
            build_contracts = store.list_contracts(phase="build")
            assert len(build_contracts) == 2
            ids = {c["contract_id"] for c in build_contracts}
            assert ids == {"ctr-build-1", "ctr-build-2"}

            design_contracts = store.list_contracts(phase="design")
            assert len(design_contracts) == 1
            assert design_contracts[0]["contract_id"] == "ctr-design-1"

    def test_list_contracts_by_slug(self, tmp_path: Path) -> None:
        """list_contracts(slug=) filters by slug."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            store.put_contract("ctr-1", phase="build", slug="alpha", body="a")
            store.put_contract("ctr-2", phase="build", slug="beta", body="b")
            store.put_contract("ctr-3", phase="design", slug="alpha", body="c")

            results = store.list_contracts(slug="alpha")
            assert len(results) == 2
            ids = {c["contract_id"] for c in results}
            assert ids == {"ctr-1", "ctr-3"}

    def test_list_contracts_by_status(self, tmp_path: Path) -> None:
        """list_contracts(status=) filters by status."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            store.put_contract("ctr-1", phase="build", slug="s", status="active")
            store.put_contract("ctr-2", phase="build", slug="s", status="archived")

            active = store.list_contracts(status="active")
            assert len(active) == 1
            assert active[0]["contract_id"] == "ctr-1"

            archived = store.list_contracts(status="archived")
            assert len(archived) == 1
            assert archived[0]["contract_id"] == "ctr-2"

    def test_list_contracts_combined_filters(self, tmp_path: Path) -> None:
        """list_contracts with multiple filters combines them with AND."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            store.put_contract("ctr-1", phase="build", slug="alpha", status="active")
            store.put_contract("ctr-2", phase="build", slug="beta", status="active")
            store.put_contract("ctr-3", phase="design", slug="alpha", status="active")

            results = store.list_contracts(phase="build", slug="alpha")
            assert len(results) == 1
            assert results[0]["contract_id"] == "ctr-1"

    def test_list_contracts_empty(self, tmp_path: Path) -> None:
        """list_contracts with no contracts returns empty list."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            assert store.list_contracts() == []

    def test_put_contract_upsert(self, tmp_path: Path) -> None:
        """put_contract updates an existing row without changing created_at."""
        import time

        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            store.put_contract("ctr-1", phase="build", slug="s", body="original")
            original = store.get_contract("ctr-1")
            original_created = original["created_at"]
            original_updated = original["updated_at"]

            time.sleep(1.1)  # timestamps are second-precision; need to cross a boundary

            store.put_contract("ctr-1", phase="build", slug="s", body="updated")
            updated = store.get_contract("ctr-1")
            assert updated["body"] == "updated"
            assert updated["created_at"] == original_created
            assert updated["updated_at"] > original_updated

    def test_put_contract_json_columns(self, tmp_path: Path) -> None:
        """JSON columns are serialized and deserialized correctly."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            store.put_contract(
                "ctr-1",
                phase="build",
                slug="s",
                domain_tags=["state-management", "api-design"],
                scope_touches=["src/a.py", "src/b.py"],
                scope_avoids=["src/x.py"],
                success_criteria=["criterion 1", "criterion 2"],
            )
            c = store.get_contract("ctr-1")
            assert c["domain_tags"] == ["state-management", "api-design"]
            assert c["scope_touches"] == ["src/a.py", "src/b.py"]
            assert c["scope_avoids"] == ["src/x.py"]
            assert c["success_criteria"] == ["criterion 1", "criterion 2"]

    def test_put_contract_readonly_refused(self, tmp_path: Path) -> None:
        """put_contract refuses on a read-only store."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as writer:
            writer.migrate()
        store = DuckDBStateStore(db, read_only=True)
        with store:
            with pytest.raises(RuntimeError, match="cannot write"):
                store.put_contract("ctr-1", phase="build", slug="s")


# ---------------------------------------------------------------------------
# TA7 — archive
# ---------------------------------------------------------------------------


class TestContractArchive:
    """TA7 — archive flips status, row stays fetchable."""

    def test_ta7_archive_flips_status(self, tmp_path: Path) -> None:
        """TA7 — archive_contract flips status to 'archived'."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            store.put_contract("ctr-1", phase="build", slug="s", body="active contract")

            result = store.archive_contract("ctr-1")
            assert result is True

            c = store.get_contract("ctr-1")
            assert c is not None
            assert c["status"] == "archived"
            assert c["body"] == "active contract"  # body unchanged

    def test_ta7_archived_row_stays_fetchable(self, tmp_path: Path) -> None:
        """TA7 — archived contract is still retrievable by contract_id."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            store.put_contract("ctr-1", phase="build", slug="s")
            store.archive_contract("ctr-1")

            # Still fetchable by ID
            c = store.get_contract("ctr-1")
            assert c is not None
            assert c["contract_id"] == "ctr-1"
            assert c["status"] == "archived"

            # Also visible in list_contracts with status filter
            archived = store.list_contracts(status="archived")
            assert len(archived) == 1
            assert archived[0]["contract_id"] == "ctr-1"

    def test_archive_nonexistent_returns_false(self, tmp_path: Path) -> None:
        """archive_contract for a non-existent ID returns False."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            result = store.archive_contract("nonexistent")
            assert result is False

    def test_archive_idempotent(self, tmp_path: Path) -> None:
        """Archiving an already-archived contract returns False (no-op)."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            store.put_contract("ctr-1", phase="build", slug="s")
            assert store.archive_contract("ctr-1") is True
            assert store.archive_contract("ctr-1") is False  # already archived

    def test_archive_readonly_refused(self, tmp_path: Path) -> None:
        """archive_contract refuses on a read-only store."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as writer:
            writer.migrate()
        store = DuckDBStateStore(db, read_only=True)
        with store:
            with pytest.raises(RuntimeError, match="cannot write"):
                store.archive_contract("ctr-1")


# ---------------------------------------------------------------------------
# TA8 — supersede
# ---------------------------------------------------------------------------


class TestContractSupersede:
    """TA8 — supersede chain; both rows readable."""

    def test_ta8_supersede_creates_new_row(self, tmp_path: Path) -> None:
        """TA8 — supersede_contract writes a new row with supersedes set."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            store.put_contract("ctr-v1", phase="build", slug="s", body="version 1")

            new_id = store.supersede_contract(
                "ctr-v1",
                new_contract_id="ctr-v2",
                phase="build",
                slug="s",
                body="version 2",
            )
            assert new_id == "ctr-v2"

            # New contract has supersedes set
            v2 = store.get_contract("ctr-v2")
            assert v2 is not None
            assert v2["supersedes"] == "ctr-v1"
            assert v2["status"] == "active"
            assert v2["body"] == "version 2"

    def test_ta8_prior_flipped_to_superseded(self, tmp_path: Path) -> None:
        """TA8 — the prior contract is flipped to 'superseded'."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            store.put_contract("ctr-v1", phase="build", slug="s")
            store.supersede_contract("ctr-v1", new_contract_id="ctr-v2", phase="build", slug="s")

            v1 = store.get_contract("ctr-v1")
            assert v1 is not None
            assert v1["status"] == "superseded"

    def test_ta8_both_rows_readable(self, tmp_path: Path) -> None:
        """TA8 — both the old and new contract remain readable."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            store.put_contract("ctr-v1", phase="build", slug="s", body="original")
            store.supersede_contract(
                "ctr-v1", new_contract_id="ctr-v2", phase="build", slug="s", body="replacement"
            )

            v1 = store.get_contract("ctr-v1")
            v2 = store.get_contract("ctr-v2")
            assert v1 is not None
            assert v2 is not None
            assert v1["status"] == "superseded"
            assert v2["status"] == "active"
            assert v1["body"] == "original"
            assert v2["body"] == "replacement"

    def test_supersede_nonexistent_raises(self, tmp_path: Path) -> None:
        """supersede_contract raises StateStoreError for a non-existent contract."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            with pytest.raises(StateStoreError, match="not found"):
                store.supersede_contract(
                    "nonexistent",
                    new_contract_id="new-one",
                    phase="build",
                    slug="s",
                )

    def test_supersede_archived_raises(self, tmp_path: Path) -> None:
        """supersede_contract refuses to supersede an archived contract."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            store.put_contract("ctr-1", phase="build", slug="s")
            store.archive_contract("ctr-1")

            with pytest.raises(StateStoreError, match="archived"):
                store.supersede_contract(
                    "ctr-1",
                    new_contract_id="ctr-2",
                    phase="build",
                    slug="s",
                )

    def test_supersede_readonly_refused(self, tmp_path: Path) -> None:
        """supersede_contract refuses on a read-only store."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as writer:
            writer.migrate()
        store = DuckDBStateStore(db, read_only=True)
        with store:
            with pytest.raises(RuntimeError, match="cannot write"):
                store.supersede_contract(
                    "ctr-1",
                    new_contract_id="ctr-2",
                    phase="build",
                    slug="s",
                )


# ---------------------------------------------------------------------------
# TA9 — correction (in-place update)
# ---------------------------------------------------------------------------


class TestContractCorrection:
    """TA9 — correction bumps updated_at without forking."""

    def test_ta9_update_bumps_updated_at(self, tmp_path: Path) -> None:
        """TA9 — update_contract bumps updated_at."""
        import time

        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            store.put_contract("ctr-1", phase="build", slug="s", body="original body")
            original = store.get_contract("ctr-1")
            original_updated = original["updated_at"]

            time.sleep(1.1)  # timestamps are second-precision; need to cross a boundary

            result = store.update_contract("ctr-1", body="corrected body")
            assert result is True

            updated = store.get_contract("ctr-1")
            assert updated["body"] == "corrected body"
            assert updated["updated_at"] > original_updated

    def test_ta9_no_fork(self, tmp_path: Path) -> None:
        """TA9 — correction does not create a new row."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            store.put_contract("ctr-1", phase="build", slug="s")
            store.update_contract("ctr-1", body="corrected")

            # Only one row exists
            all_contracts = store.list_contracts()
            assert len(all_contracts) == 1
            assert all_contracts[0]["contract_id"] == "ctr-1"

    def test_ta9_preserves_other_fields(self, tmp_path: Path) -> None:
        """TA9 — correction only changes specified fields."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            store.put_contract(
                "ctr-1",
                phase="build",
                slug="my-slug",
                domain_tags=["state-management"],
                body="original",
            )
            store.update_contract("ctr-1", body="corrected")

            c = store.get_contract("ctr-1")
            assert c["body"] == "corrected"
            assert c["phase"] == "build"  # unchanged
            assert c["slug"] == "my-slug"  # unchanged
            assert c["domain_tags"] == ["state-management"]  # unchanged
            assert c["status"] == "active"  # unchanged

    def test_ta9_update_domain_tags(self, tmp_path: Path) -> None:
        """TA9 — correction can update JSON columns."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            store.put_contract(
                "ctr-1",
                phase="build",
                slug="s",
                domain_tags=["old-tag"],
            )
            store.update_contract("ctr-1", domain_tags=["new-tag", "another-tag"])

            c = store.get_contract("ctr-1")
            assert c["domain_tags"] == ["new-tag", "another-tag"]

    def test_update_nonexistent_returns_false(self, tmp_path: Path) -> None:
        """update_contract for a non-existent ID returns False."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            result = store.update_contract("nonexistent", body="nope")
            assert result is False

    def test_update_readonly_refused(self, tmp_path: Path) -> None:
        """update_contract refuses on a read-only store."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as writer:
            writer.migrate()
        store = DuckDBStateStore(db, read_only=True)
        with store:
            with pytest.raises(RuntimeError, match="cannot write"):
                store.update_contract("ctr-1", body="nope")


# ---------------------------------------------------------------------------
# Contract + transaction integration
# ---------------------------------------------------------------------------


class TestContractTransaction:
    """Contract writes inside transaction() blocks."""

    def test_contract_write_committed_in_transaction(self, tmp_path: Path) -> None:
        """A contract written inside a transaction is visible after commit."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            with store.transaction() as tx:
                tx.put_contract("ctr-1", phase="build", slug="s")
            c = store.get_contract("ctr-1")
            assert c is not None
            assert c["contract_id"] == "ctr-1"

    def test_contract_write_rolled_back_on_exception(self, tmp_path: Path) -> None:
        """A contract written inside a rolled-back transaction disappears."""
        db = tmp_path / "test.duck"
        with DuckDBStateStore(db).open() as store:
            store.migrate()
            with pytest.raises(ValueError):
                with store.transaction() as tx:
                    tx.put_contract("ctr-1", phase="build", slug="s")
                    raise ValueError("abort")
            assert store.get_contract("ctr-1") is None
            assert store.list_contracts() == []
