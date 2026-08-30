"""Endpoint tests for ``POST /code/prune`` (single-target + batch modes).

Pins the explicit orphan path (spec R1): a row whose checkout is gone from
disk can be pruned — after the grace gate, with a dry-run preview, and only
when the absence is corroborated — while every safety refusal (live checkout,
down mount, active job, unknown row) comes back as the documented status code.
Fixture pattern follows ``test_migrate_layout.py``.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentalloy.app import create_app
from agentalloy.code_index.api.state import CodeIndexState, get_code_index_state
from agentalloy.code_index.store import code_index_paths, open_jobs
from agentalloy.config import Settings

from .conftest import FakeEmbedClient, write_fixture_repo


@pytest.fixture
def client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, CodeIndexState]]:
    monkeypatch.setenv("CODE_INDEX_ENABLED", "1")
    settings = Settings(code_index_data_dir=str(tmp_path / "code-index-data"))
    state = CodeIndexState(
        settings=settings, embed_client=FakeEmbedClient(), jobs=open_jobs(settings)
    )
    app = create_app(use_default_lifespan=False)
    app.dependency_overrides[get_code_index_state] = lambda: state
    with TestClient(app) as c:
        yield c, state
        with contextlib.suppress(AssertionError):
            _wait_idle(state)
    state.jobs.close()


def _wait_idle(state: CodeIndexState, *, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and state.tasks:
        time.sleep(0.05)


def _post(c: TestClient, **body: object) -> tuple[int, dict[str, object]]:
    resp = c.post("/code/prune", json=body)
    return resp.status_code, resp.json()


def _seed_deleted(state: CodeIndexState, slug: str, repo_path: Path, *, days_gone: float) -> None:
    """A row whose checkout is gone, already past ``days_gone`` of the clock.

    The parent directory is created: an absent path whose parent is *also* gone
    reads as an unreachable mount, not a deletion, and is never pruned.
    """
    repo_path.parent.mkdir(parents=True, exist_ok=True)
    state.jobs.upsert_repo(
        slug=slug,
        repo_path=str(repo_path),
        data_dir=str(code_index_paths(state.settings, slug, repo_path=str(repo_path)).repo_dir),
    )
    state.jobs.set_missing_since(slug, str(repo_path), int(time.time() - days_gone * 86400))


def _seed_live(state: CodeIndexState, slug: str, repo_path: Path, tmp_path: Path) -> None:
    """A row whose checkout exists on disk, with a store dir laid out current."""
    write_fixture_repo(repo_path)
    state.jobs.upsert_repo(
        slug=slug,
        repo_path=str(repo_path),
        data_dir=str(code_index_paths(state.settings, slug, repo_path=str(repo_path)).repo_dir),
    )


def _seed_unstamped(state: CodeIndexState, slug: str, repo_path: Path) -> None:
    """A gone checkout never seen absent before: no ``missing_since`` stamp."""
    repo_path.parent.mkdir(parents=True, exist_ok=True)
    state.jobs.upsert_repo(
        slug=slug,
        repo_path=str(repo_path),
        data_dir=str(code_index_paths(state.settings, slug, repo_path=str(repo_path)).repo_dir),
    )


# ---------------------------------------------------------------------------
# Single-target mode
# ---------------------------------------------------------------------------


def test_single_ripe_orphan_is_pruned(
    client: tuple[TestClient, CodeIndexState], tmp_path: Path
) -> None:
    c, state = client
    repo_path = tmp_path / "worktrees" / "demo"
    _seed_deleted(state, "demo", repo_path, days_gone=8)
    store_dir = code_index_paths(state.settings, "demo", repo_path=str(repo_path)).repo_dir
    store_dir.mkdir(parents=True)

    status, body = _post(c, slug="demo")

    assert status == 200
    entry = body["entries"][0]
    assert entry["verdict"] == "pruned"
    assert entry["row_deleted"] is True
    assert entry["store_dir"] == str(store_dir)
    assert entry["store_dir_removed"] is True
    assert body["pruned"] == 1
    assert state.jobs.get_repo("demo", repo_path=str(repo_path)) is None
    assert not store_dir.exists()


def test_single_store_dir_already_gone_is_reported_honestly(
    client: tuple[TestClient, CodeIndexState], tmp_path: Path
) -> None:
    # Partial state: the row is orphaned and past grace, but the store dir no
    # longer exists on disk. The row must be deleted and reported honestly —
    # nothing removed, not claimed removed.
    c, state = client
    repo_path = tmp_path / "worktrees" / "demo"
    _seed_deleted(state, "demo", repo_path, days_gone=8)  # no store dir created

    status, body = _post(c, slug="demo")

    assert status == 200
    entry = body["entries"][0]
    assert entry["verdict"] == "pruned"
    assert entry["row_deleted"] is True
    assert entry["store_dir"] is None
    assert entry["store_dir_removed"] is False
    assert body["pruned"] == 1
    assert state.jobs.get_repo("demo", repo_path=str(repo_path)) is None


def test_single_live_checkout_is_refused_400(
    client: tuple[TestClient, CodeIndexState], tmp_path: Path
) -> None:
    c, state = client
    repo_path = tmp_path / "worktrees" / "demo"
    _seed_live(state, "demo", repo_path, tmp_path)

    status, body = _post(c, slug="demo")

    assert status == 400
    assert "code remove" in body["detail"]
    assert state.jobs.get_repo("demo", repo_path=str(repo_path)) is not None


def test_single_down_mount_is_refused_409(
    client: tuple[TestClient, CodeIndexState], tmp_path: Path
) -> None:
    c, state = client
    # No parent created: an ancestor is gone too — a down mount, not a deletion.
    repo_path = tmp_path / "no-such-mount" / "demo"
    state.jobs.upsert_repo(
        slug="demo",
        repo_path=str(repo_path),
        data_dir=str(code_index_paths(state.settings, "demo", repo_path=str(repo_path)).repo_dir),
    )

    status, body = _post(c, slug="demo")

    assert status == 409
    assert "uncorroborated" in body["detail"]
    repo = state.jobs.get_repo("demo", repo_path=str(repo_path))
    assert repo is not None
    assert repo.missing_since is None  # never stamped


def test_single_first_sighting_stamps_and_deletes_nothing(
    client: tuple[TestClient, CodeIndexState], tmp_path: Path
) -> None:
    c, state = client
    repo_path = tmp_path / "worktrees" / "demo"
    _seed_unstamped(state, "demo", repo_path)
    t0 = int(time.time())

    status, body = _post(c, slug="demo")

    assert status == 200
    entry = body["entries"][0]
    assert entry["verdict"] == "stamped"
    assert entry["row_deleted"] is False
    assert body["stamped"] == 1
    repo = state.jobs.get_repo("demo", repo_path=str(repo_path))
    assert repo is not None
    assert repo.missing_since is not None
    assert t0 <= repo.missing_since <= int(time.time())  # stamped during this call


def test_single_not_ripe_is_refused_409(
    client: tuple[TestClient, CodeIndexState], tmp_path: Path
) -> None:
    c, state = client
    repo_path = tmp_path / "worktrees" / "demo"
    _seed_deleted(state, "demo", repo_path, days_gone=1)

    status, body = _post(c, slug="demo")

    assert status == 409
    assert "grace not elapsed" in body["detail"]
    assert state.jobs.get_repo("demo", repo_path=str(repo_path)) is not None


def test_single_forced_not_ripe_is_pruned(
    client: tuple[TestClient, CodeIndexState], tmp_path: Path
) -> None:
    c, state = client
    repo_path = tmp_path / "worktrees" / "demo"
    _seed_deleted(state, "demo", repo_path, days_gone=1)

    status, body = _post(c, slug="demo", force=True)

    assert status == 200
    assert body["forced"] is True
    assert body["entries"][0]["verdict"] == "pruned"
    assert state.jobs.get_repo("demo", repo_path=str(repo_path)) is None


def test_single_ripe_dry_run_changes_nothing(
    client: tuple[TestClient, CodeIndexState], tmp_path: Path
) -> None:
    c, state = client
    repo_path = tmp_path / "worktrees" / "demo"
    _seed_deleted(state, "demo", repo_path, days_gone=8)
    store_dir = code_index_paths(state.settings, "demo", repo_path=str(repo_path)).repo_dir
    store_dir.mkdir(parents=True)

    status, body = _post(c, slug="demo", dry_run=True)

    assert status == 200
    entry = body["entries"][0]
    assert entry["verdict"] == "pruned"
    assert entry["row_deleted"] is False
    assert entry["store_dir_removed"] is False
    # Dry run reports the dir a real prune WOULD remove (this row owns it).
    assert entry["store_dir"] == str(store_dir)
    assert state.jobs.get_repo("demo", repo_path=str(repo_path)) is not None
    assert store_dir.exists()


def test_single_dry_run_reports_preserved_dir_for_legacy_sibling(
    client: tuple[TestClient, CodeIndexState], tmp_path: Path
) -> None:
    # The dead row is in the LEGACY layout: its data_dir is the parent of the
    # live sibling's per-checkout store. A dry run must report the dir as
    # preserved (store_dir None) — not "would be removed" — while changing
    # nothing.
    c, state = client
    dead_path = tmp_path / "worktrees" / "gone"
    dead_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_dir = code_index_paths(state.settings, "demo").repo_dir
    legacy_dir.mkdir(parents=True)
    state.jobs.upsert_repo(slug="demo", repo_path=str(dead_path), data_dir=str(legacy_dir))
    state.jobs.set_missing_since("demo", str(dead_path), int(time.time() - 8 * 86400))

    live_path = tmp_path / "worktrees" / "live"
    write_fixture_repo(live_path)
    live_dir = code_index_paths(state.settings, "demo", repo_path=str(live_path)).repo_dir
    live_dir.mkdir(parents=True)
    (live_dir / "graph.overgraph").write_bytes(b"sentinel")
    state.jobs.upsert_repo(slug="demo", repo_path=str(live_path), data_dir=str(live_dir))

    status, body = _post(c, slug="demo", repo_path=str(dead_path), dry_run=True)

    assert status == 200
    entry = body["entries"][0]
    assert entry["verdict"] == "pruned"
    assert entry["store_dir"] is None
    assert entry["store_dir_removed"] is False
    assert entry["row_deleted"] is False
    assert state.jobs.get_repo("demo", repo_path=str(dead_path)) is not None
    assert legacy_dir.exists()  # nothing touched


def test_single_dry_run_first_sighting_writes_no_stamp(
    client: tuple[TestClient, CodeIndexState], tmp_path: Path
) -> None:
    c, state = client
    repo_path = tmp_path / "worktrees" / "demo"
    _seed_unstamped(state, "demo", repo_path)

    status, body = _post(c, slug="demo", dry_run=True)

    assert status == 200
    assert body["entries"][0]["verdict"] == "stamped"
    repo = state.jobs.get_repo("demo", repo_path=str(repo_path))
    assert repo is not None
    assert repo.missing_since is None


def test_single_unknown_slug_is_404(client: tuple[TestClient, CodeIndexState]) -> None:
    c, _ = client
    status, body = _post(c, slug="nope")
    assert status == 404
    assert "nope" in body["detail"]


def test_single_known_slug_wrong_path_is_404(
    client: tuple[TestClient, CodeIndexState], tmp_path: Path
) -> None:
    c, state = client
    repo_path = tmp_path / "worktrees" / "demo"
    _seed_unstamped(state, "demo", repo_path)

    status, _ = _post(c, slug="demo", repo_path=str(tmp_path / "worktrees" / "other"))

    assert status == 404


def test_single_active_job_is_refused_409(
    client: tuple[TestClient, CodeIndexState], tmp_path: Path
) -> None:
    c, state = client
    repo_path = tmp_path / "worktrees" / "demo"
    _seed_unstamped(state, "demo", repo_path)
    state.jobs.create_job(slug="demo", repo_path=str(repo_path), force_reindex=False)

    status, body = _post(c, slug="demo")

    assert status == 409
    assert "active" in body["detail"]


def test_single_legacy_sibling_dir_is_preserved(
    client: tuple[TestClient, CodeIndexState], tmp_path: Path
) -> None:
    # The dead row is in the LEGACY layout: its data_dir is repos/{slug}/, the
    # parent of the live sibling's per-checkout store. The row must go; the
    # shared directory must survive.
    c, state = client
    dead_path = tmp_path / "worktrees" / "gone"
    dead_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_dir = code_index_paths(state.settings, "demo").repo_dir
    legacy_dir.mkdir(parents=True)
    state.jobs.upsert_repo(slug="demo", repo_path=str(dead_path), data_dir=str(legacy_dir))
    state.jobs.set_missing_since("demo", str(dead_path), int(time.time() - 8 * 86400))

    live_path = tmp_path / "worktrees" / "live"
    write_fixture_repo(live_path)
    live_dir = code_index_paths(state.settings, "demo", repo_path=str(live_path)).repo_dir
    live_dir.mkdir(parents=True)
    (live_dir / "graph.overgraph").write_bytes(b"sentinel")
    state.jobs.upsert_repo(slug="demo", repo_path=str(live_path), data_dir=str(live_dir))

    status, body = _post(c, slug="demo", repo_path=str(dead_path))

    assert status == 200
    entry = body["entries"][0]
    assert entry["verdict"] == "pruned"
    assert entry["row_deleted"] is True
    assert entry["store_dir_removed"] is False
    assert entry["store_dir"] is None
    assert state.jobs.get_repo("demo", repo_path=str(dead_path)) is None
    assert (live_dir / "graph.overgraph").exists()  # live sibling untouched
    assert legacy_dir.exists()


# ---------------------------------------------------------------------------
# Batch mode (slug null)
# ---------------------------------------------------------------------------


def test_batch_mixed_verdicts_and_counts(
    client: tuple[TestClient, CodeIndexState], tmp_path: Path
) -> None:
    c, state = client
    live_path = tmp_path / "worktrees" / "live"
    _seed_live(state, "live", live_path, tmp_path)
    ripe_path = tmp_path / "worktrees" / "ripe"
    _seed_deleted(state, "ripe", ripe_path, days_gone=8)
    ripe_store = code_index_paths(state.settings, "ripe", repo_path=str(ripe_path)).repo_dir
    ripe_store.mkdir(parents=True)
    _seed_unstamped(state, "fresh", tmp_path / "worktrees" / "fresh")
    state.jobs.upsert_repo(
        slug="down",
        repo_path=str(tmp_path / "no-mount" / "down"),
        data_dir=str(
            code_index_paths(
                state.settings, "down", repo_path=str(tmp_path / "no-mount" / "down")
            ).repo_dir
        ),
    )
    waiting_path = tmp_path / "worktrees" / "waiting"
    _seed_deleted(state, "waiting", waiting_path, days_gone=1)

    status, body = _post(c)

    assert status == 200
    verdicts = {e["slug"]: e["verdict"] for e in body["entries"]}
    assert verdicts == {
        "live": "live",
        "ripe": "pruned",
        "fresh": "stamped",
        "down": "unreachable",
        "waiting": "waiting",
    }
    assert body["total"] == 5
    assert body["pruned"] == 1
    assert body["stamped"] == 1
    assert body["skipped"] == 3
    assert state.jobs.get_repo("ripe", repo_path=str(ripe_path)) is None
    assert not ripe_store.exists()
    assert state.jobs.get_repo("live", repo_path=str(live_path)) is not None
    down = state.jobs.get_repo("down", repo_path=str(tmp_path / "no-mount" / "down"))
    assert down is not None and down.missing_since is None


def test_batch_dry_run_changes_nothing(
    client: tuple[TestClient, CodeIndexState], tmp_path: Path
) -> None:
    c, state = client
    ripe_path = tmp_path / "worktrees" / "ripe"
    _seed_deleted(state, "ripe", ripe_path, days_gone=8)
    ripe_store = code_index_paths(state.settings, "ripe", repo_path=str(ripe_path)).repo_dir
    ripe_store.mkdir(parents=True)
    _seed_unstamped(state, "fresh", tmp_path / "worktrees" / "fresh")

    status, body = _post(c, dry_run=True)

    assert status == 200
    verdicts = {e["slug"]: e["verdict"] for e in body["entries"]}
    assert verdicts == {"ripe": "pruned", "fresh": "stamped"}
    assert all(e["row_deleted"] is False for e in body["entries"])
    # The ripe row owns its store dir, so a dry run names it as would-be removed.
    ripe_entry = next(e for e in body["entries"] if e["slug"] == "ripe")
    assert ripe_entry["store_dir"] == str(ripe_store)
    assert ripe_entry["store_dir_removed"] is False
    assert state.jobs.get_repo("ripe", repo_path=str(ripe_path)) is not None
    assert ripe_store.exists()
    fresh = state.jobs.get_repo("fresh", repo_path=str(tmp_path / "worktrees" / "fresh"))
    assert fresh is not None and fresh.missing_since is None


def test_batch_only_live_rows_is_a_noop(
    client: tuple[TestClient, CodeIndexState], tmp_path: Path
) -> None:
    c, state = client
    live_path = tmp_path / "worktrees" / "live"
    _seed_live(state, "live", live_path, tmp_path)

    status, body = _post(c)

    assert status == 200
    assert body["pruned"] == 0
    assert [e["verdict"] for e in body["entries"]] == ["live"]
    assert state.jobs.get_repo("live", repo_path=str(live_path)) is not None
