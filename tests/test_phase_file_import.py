"""Task 03 `phase-file-import` — the phase file is carried into the store, once.

``import_from_files`` used to store each file's entire raw text as the value.
For ``phase`` that text is multi-line flat YAML, so the row it produced was one
no reader could parse. These tests pin the rewrite: phase parses into the blob
shape, the file is removed only when its content actually landed, and a repo
whose store already disagrees keeps its file for task 08.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentalloy.api.state_router import (
    _repo_key_for,
    default_repo_root,
    get_state_store,
)
from agentalloy.api.state_router import (
    router as state_router,
)
from agentalloy.storage.state_store import (
    DuckDBStateStore,
    StateStoreError,
    open_state_store,
)

PHASE_FILE = """phase: build
mode: full
transitioned_by: proxy
free_since: 2026-07-28T09:00:00Z
"""


@pytest.fixture()
def store(tmp_path: Path) -> DuckDBStateStore:
    s = DuckDBStateStore(tmp_path / "state.duck", repo="alpha")
    s.open()
    s.migrate()
    return s


def _mirror(root: Path, **files: str) -> Path:
    ag = root / ".agentalloy"
    ag.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (ag / name).write_text(body, encoding="utf-8")
    return ag


class TestPhaseImport:
    def test_phase_lands_as_a_blob_not_raw_text(
        self, store: DuckDBStateStore, tmp_path: Path
    ) -> None:
        """The defect this task exists to fix: the row has to be readable."""
        ag = _mirror(tmp_path, phase=PHASE_FILE)

        assert store.import_from_files(ag) == {"phase": "build"}

        got = store.read_phase()
        assert got is not None
        assert got.phase == "build"
        assert got.mode == "full"
        assert got.transitioned_by == "proxy"
        assert got.paused_since == "2026-07-28T09:00:00Z"

    def test_file_is_deleted_once_imported(self, store: DuckDBStateStore, tmp_path: Path) -> None:
        ag = _mirror(tmp_path, phase=PHASE_FILE)
        store.import_from_files(ag)
        assert not (ag / "phase").exists()

    def test_second_run_is_a_no_op(self, store: DuckDBStateStore, tmp_path: Path) -> None:
        """It runs on every upgrade, so a repeat pass must import nothing."""
        ag = _mirror(tmp_path, phase=PHASE_FILE)
        store.import_from_files(ag)
        assert store.import_from_files(ag) == {}
        got = store.read_phase()
        assert got is not None and got.phase == "build"

    def test_no_mirror_at_all_is_a_no_op(self, store: DuckDBStateStore, tmp_path: Path) -> None:
        assert store.import_from_files(tmp_path / "nothing-here") == {}

    def test_bare_phase_without_metadata_imports(
        self, store: DuckDBStateStore, tmp_path: Path
    ) -> None:
        ag = _mirror(tmp_path, phase="phase: qa\n")
        assert store.import_from_files(ag) == {"phase": "qa"}
        got = store.read_phase()
        assert got is not None and got.phase == "qa"
        assert got.mode is None


class TestDiverged:
    def test_store_wins_and_the_file_survives(
        self, store: DuckDBStateStore, tmp_path: Path
    ) -> None:
        """Accepted operator consequence — plus the file is task 08's to delete.

        Until the readers move, a repo's file may be the only record of a phase
        set while the service was down, so a losing file is kept, not destroyed.
        """
        store.write_phase("ship")
        ag = _mirror(tmp_path, phase=PHASE_FILE)

        assert store.import_from_files(ag) == {}

        got = store.read_phase()
        assert got is not None and got.phase == "ship"
        assert (ag / "phase").exists()

    def test_import_is_repo_scoped(self, store: DuckDBStateStore, tmp_path: Path) -> None:
        """One store serves every repo; an import must not leak across them."""
        ag = _mirror(tmp_path, phase=PHASE_FILE)
        store.for_repo("beta").import_from_files(ag)

        beta = store.for_repo("beta").read_phase()
        assert beta is not None and beta.phase == "build"
        assert store.for_repo("alpha").read_phase() is None


class TestUnparseableFile:
    def test_raises_and_keeps_the_file(self, store: DuckDBStateStore, tmp_path: Path) -> None:
        """A file we cannot read is a thing to inspect, not to destroy."""
        ag = _mirror(tmp_path, phase="this is not a phase file\n")

        with pytest.raises(StateStoreError, match="not a readable phase file"):
            store.import_from_files(ag)

        assert (ag / "phase").exists()
        assert store.read_phase() is None


class TestOtherKinds:
    def test_session_scoped_kinds_are_skipped(
        self, store: DuckDBStateStore, tmp_path: Path
    ) -> None:
        """Their files are TSV keyed by session id, and they regenerate anyway.

        Imported as a single value they would land under a NULL session key,
        where no session-scoped reader would ever match them.
        """
        ag = _mirror(tmp_path, announced="__free__\tsess-1\n", phase=PHASE_FILE)
        imported = store.import_from_files(ag)
        assert "announced" not in imported
        assert store.read("announced") is None
        assert (ag / "announced").exists()

    def test_directory_shaped_kind_is_skipped(
        self, store: DuckDBStateStore, tmp_path: Path
    ) -> None:
        """``approved`` is a directory of per-phase markers — reading it raises."""
        ag = _mirror(tmp_path, phase=PHASE_FILE)
        (ag / "approved").mkdir()
        (ag / "approved" / "design").write_text("", encoding="utf-8")

        assert store.import_from_files(ag) == {"phase": "build"}
        assert store.read("approved") is None

    def test_repo_scoped_file_kind_imports(self, store: DuckDBStateStore, tmp_path: Path) -> None:
        ag = _mirror(tmp_path, cursor="active/build/03-import.md\n")
        assert store.import_from_files(ag) == {"cursor": "active/build/03-import.md"}
        assert store.read("cursor") == "active/build/03-import.md"


class TestImportRoute:
    """The service holds the write lock, so the migration is an HTTP call."""

    @pytest.fixture()
    def wired(self, tmp_path: Path):
        db = tmp_path / "state.duck"
        s = open_state_store(db, repo=_repo_key_for(str(default_repo_root())))
        app = FastAPI()
        app.include_router(state_router)
        app.dependency_overrides[get_state_store] = lambda: s
        try:
            yield s, TestClient(app)
        finally:
            s.close()

    def test_route_imports_the_repo_it_is_pointed_at(self, wired, tmp_path: Path) -> None:
        store, client = wired
        repo = tmp_path / "somerepo"
        _mirror(repo, phase=PHASE_FILE)

        resp = client.post("/state/import-files", params={"repo_root": str(repo)})

        assert resp.status_code == 200
        assert resp.json() == {"imported": {"phase": "build"}}
        assert not (repo / ".agentalloy" / "phase").exists()
        assert store.for_repo(_repo_key_for(str(repo))).read_phase() is not None

    def test_route_on_a_repo_with_no_mirror(self, wired, tmp_path: Path) -> None:
        _store, client = wired
        resp = client.post("/state/import-files", params={"repo_root": str(tmp_path / "bare")})
        assert resp.status_code == 200
        assert resp.json() == {"imported": {}}

    def test_unparseable_file_is_a_422_not_a_500(self, wired, tmp_path: Path) -> None:
        _store, client = wired
        repo = tmp_path / "badrepo"
        _mirror(repo, phase="garbage\n")

        resp = client.post("/state/import-files", params={"repo_root": str(repo)})

        assert resp.status_code == 422
        assert (repo / ".agentalloy" / "phase").exists()
