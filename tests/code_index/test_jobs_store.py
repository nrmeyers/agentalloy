"""CodeIndexJobsStore — job lifecycle, heartbeat, cancel, sweep, registry."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from agentalloy.code_index.store.jobs_store import CodeIndexJobsStore


@pytest.fixture
def store(tmp_path: Path) -> Iterator[CodeIndexJobsStore]:
    s = CodeIndexJobsStore(tmp_path / "jobs.sqlite")
    yield s
    s.close()


def test_wal_mode(store: CodeIndexJobsStore) -> None:
    assert store.journal_mode().lower() == "wal"


def test_lifecycle_create_running_done(store: CodeIndexJobsStore) -> None:
    job = store.create_job(slug="org__repo", repo_path="/src/repo", worker_token="t1")
    assert job.status == "running" and job.phase == "queued"
    assert job.slug == "org__repo"
    assert job.progress_pct == 0.0
    assert not job.force_reindex and not job.cancel_requested

    store.update_progress(
        job.job_id, phase="parsing", progress_pct=42.5, files_total=10, files_done=4
    )
    mid = store.get_job(job.job_id)
    assert mid is not None
    assert mid.phase == "parsing"
    assert mid.progress_pct == pytest.approx(42.5)
    assert (mid.files_total, mid.files_done) == (10, 4)

    store.mark_done(job.job_id, symbol_count=100, edge_count=250, embedding_count=90)
    done = store.get_job(job.job_id)
    assert done is not None
    assert done.status == "done" and done.phase == "done"
    assert done.progress_pct == 100.0
    assert (done.symbol_count, done.edge_count, done.embedding_count) == (100, 250, 90)
    assert done.finished_at is not None
    # Terminal transition records an event.
    events = store.list_job_events(job.job_id)
    assert any("done" in str(e["message"]) for e in events)


def test_get_job_missing(store: CodeIndexJobsStore) -> None:
    assert store.get_job("nope") is None


def test_heartbeat_advances_updated_at_only(store: CodeIndexJobsStore) -> None:
    job = store.create_job(slug="s", repo_path="/r")
    store.update_progress(job.job_id, phase="writing", progress_pct=50.0)
    before = store.get_job(job.job_id)
    assert before is not None
    # Force a visible delta regardless of clock resolution.
    store.conn.execute(
        "UPDATE jobs SET updated_at = updated_at - 100 WHERE job_id = ?", (job.job_id,)
    )
    store.touch_heartbeat(job.job_id)
    after = store.get_job(job.job_id)
    assert after is not None
    assert after.updated_at >= before.updated_at
    assert after.phase == "writing" and after.progress_pct == pytest.approx(50.0)
    # No-op on terminal rows.
    store.mark_done(job.job_id, symbol_count=0, edge_count=0, embedding_count=0)
    store.touch_heartbeat(job.job_id)  # must not raise


def test_cancel_flow(store: CodeIndexJobsStore) -> None:
    job = store.create_job(slug="s", repo_path="/r")
    assert not store.is_cancel_requested(job.job_id)
    assert store.request_cancel(job.job_id) is True
    assert store.is_cancel_requested(job.job_id) is True
    store.mark_failed(job.job_id, error="cancelled by user", terminal_status="cancelled")
    got = store.get_job(job.job_id)
    assert got is not None and got.status == "cancelled"
    # Cancelling a terminal (or absent) job is a no-op.
    assert store.request_cancel(job.job_id) is False
    assert store.request_cancel("missing") is False
    assert store.is_cancel_requested("missing") is False


def test_mark_failed(store: CodeIndexJobsStore) -> None:
    job = store.create_job(slug="s", repo_path="/r")
    store.mark_failed(job.job_id, error="parse exploded")
    got = store.get_job(job.job_id)
    assert got is not None
    assert got.status == "failed" and got.error == "parse exploded"
    assert got.finished_at is not None


def test_sweep_interrupted_by_worker_token(store: CodeIndexJobsStore) -> None:
    stale = store.create_job(slug="s1", repo_path="/r1", worker_token="dead-proc")
    tokenless = store.create_job(slug="s2", repo_path="/r2", worker_token=None)
    mine = store.create_job(slug="s3", repo_path="/r3", worker_token="live-proc")
    terminal = store.create_job(slug="s4", repo_path="/r4", worker_token="dead-proc")
    store.mark_done(terminal.job_id, symbol_count=1, edge_count=1, embedding_count=1)

    assert store.sweep_interrupted("live-proc") == 2  # stale + tokenless

    for job_id, expected in (
        (stale.job_id, "interrupted"),
        (tokenless.job_id, "interrupted"),
        (mine.job_id, "running"),
        (terminal.job_id, "done"),
    ):
        got = store.get_job(job_id)
        assert got is not None and got.status == expected


def test_list_jobs_and_find_active(store: CodeIndexJobsStore) -> None:
    a = store.create_job(slug="repo-a", repo_path="/a")
    b = store.create_job(slug="repo-b", repo_path="/b")
    store.mark_done(a.job_id, symbol_count=0, edge_count=0, embedding_count=0)

    assert {j.job_id for j in store.list_jobs()} == {a.job_id, b.job_id}
    assert [j.job_id for j in store.list_jobs(slug="repo-a")] == [a.job_id]
    assert [j.job_id for j in store.list_jobs(status={"running"})] == [b.job_id]
    assert store.list_jobs(slug="repo-a", status={"running"}) == []

    active = store.find_active("repo-b")
    assert active is not None and active.job_id == b.job_id
    assert store.find_active("repo-a") is None


def test_job_events_ordering(store: CodeIndexJobsStore) -> None:
    job = store.create_job(slug="s", repo_path="/r")
    store.record_event(job.job_id, "info", "first")
    store.record_event(job.job_id, "warn", "second")
    events = store.list_job_events(job.job_id)
    assert [e["message"] for e in events] == ["first", "second"]
    assert [e["level"] for e in events] == ["info", "warn"]


def test_indexed_repos_registry(store: CodeIndexJobsStore) -> None:
    store.upsert_repo(slug="org__x", repo_path="/src/x", data_dir="/data/repos/org__x")
    repo = store.get_repo("org__x")
    assert repo is not None
    assert repo.repo_path == "/src/x"
    assert repo.data_dir == "/data/repos/org__x"
    assert repo.last_indexed_at is None and repo.head_sha is None

    # Upsert updates paths but preserves created_at / last_indexed_at.
    store.upsert_repo(slug="org__x", repo_path="/moved/x", data_dir="/data/repos/org__x")
    moved = store.get_repo("org__x")
    assert moved is not None
    assert moved.repo_path == "/moved/x"
    assert moved.created_at == repo.created_at
    assert moved.last_indexed_at is None

    assert store.mark_indexed("org__x", head_sha="abc123") is True
    indexed = store.get_repo("org__x")
    assert indexed is not None
    assert indexed.last_indexed_at is not None and indexed.head_sha == "abc123"
    assert store.mark_indexed("missing") is False

    store.upsert_repo(slug="org__y", repo_path="/src/y", data_dir="/data/repos/org__y")
    assert {r.slug for r in store.list_repos()} == {"org__x", "org__y"}

    assert store.delete_repo("org__y") is True
    assert store.delete_repo("org__y") is False
    assert store.get_repo("org__y") is None
