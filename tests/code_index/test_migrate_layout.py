"""``POST /code/migrate-layout`` — the automatic, no-opt-in store migration.

The contract this pins: an upgrade migrates EVERY registered repo onto the
per-checkout ``repos/{slug}/{path_key}/`` layout without asking, is idempotent
across repeat runs, and does not accumulate dead registry rows.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentalloy.app import create_app
from agentalloy.code_index.api.state import CodeIndexState, get_code_index_state
from agentalloy.code_index.store import code_index_paths, open_jobs
from agentalloy.config import Settings

from .conftest import FakeEmbedClient, write_fixture_repo


def _wait_for(predicate: Callable[[], bool], *, timeout_s: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition not met within timeout")


def _wait_for_terminal(c: TestClient, job_id: str) -> None:
    _wait_for(
        lambda: c.get(f"/code/index/{job_id}/status").json()["state"] not in ("queued", "running")
    )


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
            _wait_for(lambda: not state.tasks, timeout_s=10.0)
    state.jobs.close()


def _legacy_dir(state: CodeIndexState, slug: str) -> Path:
    """The pre-migration ``repos/{slug}/`` path (no repo_path scoping)."""
    return code_index_paths(state.settings, slug).repo_dir


def _current_dir(state: CodeIndexState, slug: str, repo_path: Path) -> Path:
    return code_index_paths(state.settings, slug, repo_path=str(repo_path)).repo_dir


def _seed_legacy(state: CodeIndexState, slug: str, repo_path: Path) -> None:
    """A registry row in the old layout, as an install predating the migration."""
    state.jobs.upsert_repo(
        slug=slug, repo_path=str(repo_path), data_dir=str(_legacy_dir(state, slug))
    )


def _seed_deleted(state: CodeIndexState, slug: str, repo_path: Path, *, days_gone: float) -> None:
    """A row whose checkout is gone, already past ``days_gone`` of the clock.

    The parent directory is created: an absent path whose parent is *also* gone
    reads as an unreachable mount, not a deletion, and is never pruned.
    """
    repo_path.parent.mkdir(parents=True, exist_ok=True)
    _seed_legacy(state, slug, repo_path)
    state.jobs.set_missing_since(slug, str(repo_path), int(time.time() - days_gone * 86400))


def _seed_current(state: CodeIndexState, slug: str, repo_path: Path) -> None:
    state.jobs.upsert_repo(
        slug=slug, repo_path=str(repo_path), data_dir=str(_current_dir(state, slug, repo_path))
    )


def _post(c: TestClient, **body: object) -> dict[str, object]:
    resp = c.post("/code/migrate-layout", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _verdicts(body: dict[str, object]) -> dict[str, str]:
    entries = body["entries"]
    assert isinstance(entries, list)
    return {e["slug"]: e["verdict"] for e in entries}


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_classifies_legacy_current_and_missing(
    client: tuple[TestClient, CodeIndexState], tmp_path: Path
) -> None:
    c, state = client
    live = tmp_path / "live"
    write_fixture_repo(live)
    done = tmp_path / "already"
    write_fixture_repo(done)

    _seed_legacy(state, "old-one", live)
    _seed_current(state, "new-one", done)
    _seed_deleted(state, "ghost", tmp_path / "checkouts" / "deleted", days_gone=30)

    body = _post(c, dry_run=True)

    assert body["total"] == 3
    assert _verdicts(body) == {"old-one": "legacy", "new-one": "current", "ghost": "missing"}
    assert body["legacy"] == 1
    assert body["current"] == 1


def test_dry_run_changes_nothing(client: tuple[TestClient, CodeIndexState], tmp_path: Path) -> None:
    c, state = client
    live = tmp_path / "live"
    write_fixture_repo(live)
    _seed_legacy(state, "old-one", live)
    gone = tmp_path / "checkouts" / "gone"
    gone.parent.mkdir(parents=True)
    _seed_legacy(state, "ghost", gone)

    body = _post(c, dry_run=True)

    assert body["jobs"] == []
    assert body["pruned"] == 0
    assert {r.slug for r in state.jobs.list_repos()} == {"old-one", "ghost"}
    assert all(e["action"] == "none" for e in body["entries"])
    # Not even the grace clock is started on a dry run.
    assert all(r.missing_since is None for r in state.jobs.list_repos())


# ---------------------------------------------------------------------------
# Pruning dead rows — without this every future upgrade retries them forever
# ---------------------------------------------------------------------------


def test_missing_repo_rows_are_pruned_once_the_absence_persists(
    client: tuple[TestClient, CodeIndexState], tmp_path: Path
) -> None:
    c, state = client
    _seed_deleted(state, "ghost-a", tmp_path / "checkouts" / "gone-a", days_gone=30)
    _seed_deleted(state, "ghost-b", tmp_path / "checkouts" / "gone-b", days_gone=8)

    body = _post(c)

    assert body["pruned"] == 2
    assert state.jobs.list_repos() == []


def test_first_sighting_only_starts_the_clock(
    client: tuple[TestClient, CodeIndexState], tmp_path: Path
) -> None:
    """The gate: one unattended upgrade during a transient absence must not
    delete a live index. Absence has to persist across runs to count."""
    c, state = client
    gone = tmp_path / "checkouts" / "gone"
    gone.parent.mkdir(parents=True)
    _seed_legacy(state, "ghost", gone)

    body = _post(c)

    assert body["pruned"] == 0
    assert _verdicts(body) == {"ghost": "missing"}
    assert [e["action"] for e in body["entries"]] == ["stamped"]
    rows = state.jobs.list_repos()
    assert len(rows) == 1 and rows[0].missing_since is not None

    # Still inside the grace window on the next run: still no deletion.
    again = _post(c)
    assert again["pruned"] == 0
    assert [e["action"] for e in again["entries"]] == ["waiting"]
    assert len(state.jobs.list_repos()) == 1


def test_a_returning_checkout_clears_the_clock(
    client: tuple[TestClient, CodeIndexState], tmp_path: Path
) -> None:
    """A repo that comes back must not carry an old stamp into a later run."""
    c, state = client
    live = tmp_path / "checkouts" / "live"
    _seed_deleted(state, "demo", live, days_gone=30)

    write_fixture_repo(live)  # the mount is back before the migration runs
    body = _post(c)
    for job in body["jobs"]:  # pyright: ignore[reportGeneralTypeIssues]
        _wait_for_terminal(c, job["id"])

    assert body["pruned"] == 0
    rows = state.jobs.list_repos()
    assert len(rows) == 1 and rows[0].missing_since is None


def test_absent_with_a_missing_ancestor_is_never_pruned(
    client: tuple[TestClient, CodeIndexState], tmp_path: Path
) -> None:
    """A down mount takes ancestors with it — that is not evidence of deletion,
    so it is neither pruned nor allowed to start the clock, however long."""
    c, state = client
    _seed_legacy(state, "on-nfs", tmp_path / "unmounted" / "vol" / "repo")
    state.jobs.set_missing_since("on-nfs", str(tmp_path / "unmounted" / "vol" / "repo"), 0)

    body = _post(c)

    assert _verdicts(body) == {"on-nfs": "unreachable"}
    assert body["pruned"] == 0
    assert body["unreachable"] == 1
    assert [r.slug for r in state.jobs.list_repos()] == ["on-nfs"]


def test_pruning_a_dead_sibling_spares_the_live_checkout(
    client: tuple[TestClient, CodeIndexState], tmp_path: Path
) -> None:
    """The registry is keyed (slug, repo_path): checkouts share a slug.

    A dead row still in the legacy layout owns ``repos/{slug}/`` — the parent of
    every sibling's store. Pruning it by slug alone would delete a live
    checkout's index and its registry row along with the dead one.
    """
    c, state = client
    live = tmp_path / "live"
    write_fixture_repo(live)
    gone = tmp_path / "gone"

    _seed_current(state, "demo", live)
    live_dir = _current_dir(state, "demo", live)
    live_dir.mkdir(parents=True, exist_ok=True)
    (live_dir / "graph.duck").write_bytes(b"stub")
    _seed_deleted(state, "demo", gone, days_gone=30)  # same slug, deleted checkout

    body = _post(c)

    assert body["pruned"] == 1
    assert [(r.slug, r.repo_path) for r in state.jobs.list_repos()] == [("demo", str(live))]
    assert (live_dir / "graph.duck").exists()


def test_keep_missing_leaves_dead_rows_alone(
    client: tuple[TestClient, CodeIndexState], tmp_path: Path
) -> None:
    c, state = client
    _seed_deleted(state, "ghost", tmp_path / "checkouts" / "gone", days_gone=30)

    body = _post(c, prune_missing=False)

    assert body["pruned"] == 0
    assert _verdicts(body) == {"ghost": "missing"}
    assert [r.slug for r in state.jobs.list_repos()] == ["ghost"]


# ---------------------------------------------------------------------------
# The migration itself
# ---------------------------------------------------------------------------


def test_legacy_repo_reindexes_into_the_per_checkout_layout(
    client: tuple[TestClient, CodeIndexState], tmp_path: Path
) -> None:
    c, state = client
    live = tmp_path / "live"
    write_fixture_repo(live)
    _seed_legacy(state, "demo", live)

    body = _post(c)
    jobs = body["jobs"]
    assert isinstance(jobs, list) and len(jobs) == 1

    job_id = jobs[0]["id"]
    _wait_for(
        lambda: c.get(f"/code/index/{job_id}/status").json()["state"] not in ("queued", "running")
    )
    assert c.get(f"/code/index/{job_id}/status").json()["state"] == "done"

    rows = state.jobs.list_repos()
    assert len(rows) == 1
    assert Path(rows[0].data_dir) == _current_dir(state, rows[0].slug, live)
    assert Path(rows[0].data_dir).is_dir()


def test_legacy_directory_is_left_in_place(
    client: tuple[TestClient, CodeIndexState], tmp_path: Path
) -> None:
    """Additive by design: the old layout stays readable as a fallback, so an
    interrupted migration never leaves a repo with no index at all."""
    c, state = client
    live = tmp_path / "live"
    write_fixture_repo(live)
    _seed_legacy(state, "demo", live)
    legacy = _legacy_dir(state, "demo")
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "graph.duck").write_bytes(b"stub")

    body = _post(c)
    for job in body["jobs"]:  # pyright: ignore[reportGeneralTypeIssues]
        _wait_for_terminal(c, job["id"])

    assert (legacy / "graph.duck").exists()


def test_second_run_is_a_no_op(client: tuple[TestClient, CodeIndexState], tmp_path: Path) -> None:
    """Idempotence is what makes 'every upgrade runs it' affordable."""
    c, state = client
    live = tmp_path / "live"
    write_fixture_repo(live)
    _seed_legacy(state, "demo", live)

    first = _post(c)
    for job in first["jobs"]:  # pyright: ignore[reportGeneralTypeIssues]
        _wait_for_terminal(c, job["id"])

    second = _post(c)
    assert second["legacy"] == 0
    assert second["jobs"] == []
    assert second["current"] == 1


def test_empty_registry_is_clean(client: tuple[TestClient, CodeIndexState]) -> None:
    c, _ = client
    body = _post(c)
    assert body == {
        "dry_run": False,
        "total": 0,
        "current": 0,
        "legacy": 0,
        "pruned": 0,
        "unreachable": 0,
        "busy": 0,
        "entries": [],
        "jobs": [],
    }
