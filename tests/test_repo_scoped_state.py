"""Task 11 `repo-scoped-state` — the repo key stops coming from the DB filename.

``_repo()`` returned ``Path(db_path).stem``. The service opens exactly one
``state.duck``, so every repo on the machine wrote ``repo='state'``. Per-repo
isolation existed only because ``.agentalloy/phase`` was a per-repo file —
which tasks 04-08 delete. These tests pin the replacement: an explicit repo key,
a scoped view over the one shared connection, and a migration that re-keys the
rows written under the old bucket.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentalloy.storage.state_store import (
    LEGACY_REPO_KEY,
    DuckDBStateStore,
    open_state_store,
)


@pytest.fixture()
def store(tmp_path: Path) -> DuckDBStateStore:
    s = DuckDBStateStore(tmp_path / "state.duck", repo="alpha")
    s.open()
    s.migrate()
    return s


class TestExplicitRepoKey:
    def test_repo_is_the_constructor_value_not_the_filename(self, store: DuckDBStateStore) -> None:
        store.write_phase("design")
        rows = store.execute("SELECT repo FROM sdd_state WHERE kind='phase'")
        assert rows and rows[0][0] == "alpha"

    def test_filename_is_not_a_fallback(self, tmp_path: Path) -> None:
        """Omitting ``repo`` must not silently resurrect the filename key.

        The old behaviour was indistinguishable from working until two repos
        talked to one service, so the default has to be a *named* bucket rather
        than something that looks repo-shaped.
        """
        s = DuckDBStateStore(tmp_path / "state.duck").open()
        s.migrate()
        s.write_phase("design")
        rows = s.execute("SELECT repo FROM sdd_state WHERE kind='phase'")
        assert rows and rows[0][0] == LEGACY_REPO_KEY
        s.close()


class TestScopedViews:
    def test_two_repos_hold_independent_phases(self, store: DuckDBStateStore) -> None:
        """The property the phase *file* was providing, now in the store."""
        store.for_repo("alpha").write_phase("design")
        store.for_repo("beta").write_phase("qa")

        a = store.for_repo("alpha").read_phase()
        b = store.for_repo("beta").read_phase()
        assert a is not None and a.phase == "design"
        assert b is not None and b.phase == "qa"

    def test_clear_in_one_repo_leaves_the_other(self, store: DuckDBStateStore) -> None:
        store.for_repo("alpha").write_phase("design")
        store.for_repo("beta").write_phase("qa")
        assert store.for_repo("alpha").clear("phase") == 1
        assert store.for_repo("alpha").read_phase() is None
        assert store.for_repo("beta").read_phase() is not None

    def test_contracts_are_repo_scoped(self, store: DuckDBStateStore) -> None:
        """`sdd_contract` shared one global bucket by the same defect."""
        store.for_repo("alpha").put_contract("build/x", phase="build", slug="x")
        store.for_repo("beta").put_contract("build/y", phase="build", slug="y")

        alpha = [c["contract_id"] for c in store.for_repo("alpha").list_contracts()]
        beta = [c["contract_id"] for c in store.for_repo("beta").list_contracts()]
        assert alpha == ["build/x"]
        assert beta == ["build/y"]
        assert store.for_repo("beta").get_contract("build/x") is None

    def test_same_contract_id_in_two_repos_does_not_collide(self, store: DuckDBStateStore) -> None:
        """`PRIMARY KEY (repo, contract_id)` only helps once repo is real.

        Two repos both running this migration would otherwise fight over
        ``build/phase-blob-shape``.
        """
        store.for_repo("alpha").put_contract("build/dup", phase="build", slug="dup", body="A")
        store.for_repo("beta").put_contract("build/dup", phase="build", slug="dup", body="B")

        a = store.for_repo("alpha").get_contract("build/dup")
        b = store.for_repo("beta").get_contract("build/dup")
        assert a is not None and a["body"] == "A"
        assert b is not None and b["body"] == "B"

    def test_view_shares_the_connection(self, store: DuckDBStateStore) -> None:
        """A view must not open a second handle — DuckDB is single-writer."""
        assert store.for_repo("beta").conn is store.conn

    def test_nested_transaction_guard_is_connection_wide(self, store: DuckDBStateStore) -> None:
        """The re-entrancy guard has to be shared, not copied per view.

        Two views over one connection issuing BEGIN twice is a DuckDB error,
        so the guard cannot live in per-view state.
        """
        with store.for_repo("alpha").transaction():
            with pytest.raises(RuntimeError, match="nested transaction"):
                with store.for_repo("beta").transaction():
                    pass

    def test_view_of_a_view_rescopes(self, store: DuckDBStateStore) -> None:
        assert store.for_repo("beta").for_repo("gamma").repo == "gamma"


class TestRekeyMigration:
    def test_legacy_rows_move_to_the_real_slug(self, tmp_path: Path) -> None:
        db = tmp_path / "state.duck"
        seed = DuckDBStateStore(db).open()
        seed.migrate()
        seed.write_phase("design")
        seed.put_contract("build/x", phase="build", slug="x")
        seed.close()

        s = DuckDBStateStore(db, repo="agentalloy").open()
        s.migrate()
        assert s.rekey_legacy_rows("agentalloy") == 2

        got = s.read_phase()
        assert got is not None and got.phase == "design"
        assert [c["contract_id"] for c in s.list_contracts()] == ["build/x"]
        assert (
            s.execute("SELECT count(*) FROM sdd_state WHERE repo = ?", (LEGACY_REPO_KEY,))[0][0]
            == 0
        )
        s.close()

    def test_rekey_is_idempotent(self, tmp_path: Path) -> None:
        """It runs on every service start; a second pass must be a no-op."""
        db = tmp_path / "state.duck"
        s = DuckDBStateStore(db, repo="agentalloy").open()
        s.migrate()
        s.for_repo(LEGACY_REPO_KEY).write_phase("design")
        assert s.rekey_legacy_rows("agentalloy") == 1
        assert s.rekey_legacy_rows("agentalloy") == 0
        s.close()

    def test_rekey_does_not_clobber_an_existing_target_row(self, tmp_path: Path) -> None:
        """A repo that already wrote under its real slug wins over the legacy bucket.

        The legacy bucket is a merge of every repo that ever talked to the
        service; a row written deliberately under the real key is the better
        record, so the migration drops the legacy one rather than overwriting.
        """
        db = tmp_path / "state.duck"
        s = DuckDBStateStore(db, repo="agentalloy").open()
        s.migrate()
        s.for_repo(LEGACY_REPO_KEY).write_phase("design")
        s.write_phase("ship")

        s.rekey_legacy_rows("agentalloy")

        got = s.read_phase()
        assert got is not None and got.phase == "ship"
        assert (
            s.execute("SELECT count(*) FROM sdd_state WHERE repo = ?", (LEGACY_REPO_KEY,))[0][0]
            == 0
        )
        s.close()

    def test_read_only_store_refuses_to_rekey(self, tmp_path: Path) -> None:
        db = tmp_path / "state.duck"
        DuckDBStateStore(db).open().migrate()

        ro = DuckDBStateStore(db, read_only=True).open()
        with pytest.raises(RuntimeError, match="read-only"):
            ro.rekey_legacy_rows("agentalloy")
        ro.close()


class TestOpenStateStore:
    def test_repo_is_threaded_through(self, tmp_path: Path) -> None:
        s = open_state_store(tmp_path / "state.duck", repo="alpha")
        assert s.repo == "alpha"
        s.close()
