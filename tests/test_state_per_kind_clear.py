"""Task 02 `state-per-kind-clear` — the store needs an equivalent of ``unlink()``.

The reset paths (``wire``, ``add``) and ``run_phase_clear`` used to make a kind
absent by deleting its file. Once phase lives only in the store, "absent" has to
be expressible there too — writing ``""`` is not the same thing, because a row
holding an empty value still reads as present to anything checking for ``None``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentalloy.storage.state_store import DuckDBStateStore


@pytest.fixture()
def store(tmp_path: Path) -> DuckDBStateStore:
    s = DuckDBStateStore(tmp_path / "state.db")
    s.open()
    s.migrate()
    return s


class TestClear:
    def test_clear_removes_the_row(self, store: DuckDBStateStore) -> None:
        store.write_phase("design")
        assert store.clear("phase") == 1
        assert store.read("phase") is None
        assert store.read_phase() is None

    def test_absent_kind_is_not_an_error(self, store: DuckDBStateStore) -> None:
        """Reset paths run unconditionally, so clearing nothing must be a no-op."""
        assert store.clear("phase") == 0

    def test_clear_is_scoped_to_the_kind(self, store: DuckDBStateStore) -> None:
        store.write_phase("design")
        store.write("cursor", "task-01")
        store.clear("phase")
        assert store.read("cursor") == "task-01"

    def test_empty_value_is_not_absence(self, store: DuckDBStateStore) -> None:
        """The distinction this method exists for.

        Writing ``""`` leaves a row that reads as present; only ``clear`` makes
        the kind genuinely absent.
        """
        store.write("phase", "")
        assert store.read("phase") is not None
        store.clear("phase")
        assert store.read("phase") is None


class TestClearReleasesLease:
    def test_leased_kind_can_be_cleared_and_relet(self, store: DuckDBStateStore) -> None:
        """AC: clearing a leased kind must not strand the lease.

        ``acquire_lease`` refuses to create rows, so if a cleared kind left a
        lease behind, the next writer would inherit a conflict against a row
        that no longer exists.
        """
        store.write_phase("design", owner="sess-1")
        store.acquire_lease("phase", "sess-1")
        assert store.clear("phase") == 1

        # A different session writes the kind fresh and sees no inherited owner.
        result = store.write_phase("build", owner="sess-2")
        assert result.conflict is None
        row = store.execute(
            "SELECT owner FROM sdd_state WHERE kind='phase' AND session_key IS NULL"
        )
        assert row and row[0][0] == "sess-2"


class TestSessionScoping:
    def test_session_scoped_clear_targets_one_session(self, store: DuckDBStateStore) -> None:
        store.write("announced", "design", session_key="a")
        store.write("announced", "design", session_key="b")
        assert store.clear("announced", session_key="a") == 1
        assert store.read("announced", session_key="a") is None
        assert store.read("announced", session_key="b") == "design"

    def test_all_sessions_sweeps_every_row(self, store: DuckDBStateStore) -> None:
        """What a reset needs: no single key names "all of them"."""
        store.write("announced", "design", session_key="a")
        store.write("announced", "design", session_key="b")
        assert store.clear("announced", all_sessions=True) == 2
        assert store.read("announced", session_key="a") is None
        assert store.read("announced", session_key="b") is None

    def test_repo_scoped_clear_leaves_session_rows(self, store: DuckDBStateStore) -> None:
        """Default scoping must not silently behave like ``all_sessions``."""
        store.write("announced", "repo-level")
        store.write("announced", "design", session_key="a")
        assert store.clear("announced") == 1
        assert store.read("announced", session_key="a") == "design"

    def test_all_sessions_with_key_is_rejected(self, store: DuckDBStateStore) -> None:
        with pytest.raises(ValueError, match="cannot take a session_key"):
            store.clear("announced", session_key="a", all_sessions=True)


class TestReadOnly:
    def test_read_only_store_refuses(self, tmp_path: Path) -> None:
        writable = DuckDBStateStore(tmp_path / "ro.db")
        writable.open()
        writable.migrate()
        writable.write_phase("design")
        writable.close()

        ro = DuckDBStateStore(tmp_path / "ro.db", read_only=True)
        ro.open()
        with pytest.raises(RuntimeError, match="read-only"):
            ro.clear("phase")
        ro.close()
