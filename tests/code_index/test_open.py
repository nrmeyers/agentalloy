"""code_index.store.open — paths layout, roles, locks, remove_repo, jobs."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from agentalloy.code_index.store.graph_store import DuckDBCodeGraphStore
from agentalloy.code_index.store.open import (
    code_index_paths,
    open_code_index,
    open_jobs,
    remove_repo,
    slug_write_lock,
)
from agentalloy.config import Settings
from agentalloy.storage.protocols import CodeSymbol


def make_settings(tmp_path: Path) -> Settings:
    return Settings(code_index_data_dir=str(tmp_path / "code-index"))


def make_symbol(qn: str) -> CodeSymbol:
    return CodeSymbol(
        qualified_name=qn,
        kind="Function",
        name=qn.rsplit(".", 1)[-1],
        file_path="m.py",
        start_line=1,
        end_line=2,
        docstring=None,
        decorators=[],
        is_exported=None,
        is_async=False,
        is_generator=False,
        source_code=None,
    )


def test_paths_layout(tmp_path: Path) -> None:
    s = make_settings(tmp_path)
    paths = code_index_paths(s, "org__repo")
    root = Path(s.code_index_data_dir)
    assert paths.root == root
    assert paths.repo_dir == root / "repos" / "org__repo"
    assert paths.graph_path == root / "repos" / "org__repo" / "graph.duck"
    assert paths.vectors_path == root / "repos" / "org__repo" / "vectors.lance"
    assert paths.cache_dir == root / "repos" / "org__repo" / "cache"
    assert paths.jobs_path == root / "jobs.sqlite"


def test_open_roles(tmp_path: Path) -> None:
    s = make_settings(tmp_path)
    paths = code_index_paths(s, "repo")

    writer = open_code_index(s, "repo", role="writer")
    assert writer.slug == "repo"
    assert paths.graph_path.exists()
    assert paths.cache_dir.is_dir()
    writer.graph.upsert_symbols([make_symbol("m.fn")])
    writer.close()

    # service role is also read-write (the service IS the code-index writer).
    service = open_code_index(s, "repo", role="service")
    service.graph.set_meta("k", "v")
    service.close()

    reader = open_code_index(s, "repo", role="reader")
    got = reader.graph.symbol("m.fn")
    assert got is not None and got.name == "fn"
    # A reader handle must reject writes (DuckDB read_only) and migration.
    with pytest.raises(duckdb.Error):
        reader.graph.upsert_symbols([make_symbol("m.other")])
    assert isinstance(reader.graph, DuckDBCodeGraphStore)
    with pytest.raises(RuntimeError):
        reader.graph.migrate()
    reader.close()


def test_handles_close_is_idempotent(tmp_path: Path) -> None:
    s = make_settings(tmp_path)
    handles = open_code_index(s, "repo", role="writer")
    handles.close()
    handles.close()  # second close must not raise


def test_slug_write_lock_registry() -> None:
    a1 = slug_write_lock("slug-a")
    a2 = slug_write_lock("slug-a")
    b = slug_write_lock("slug-b")
    assert a1 is a2
    assert a1 is not b
    with a1:  # usable as a plain threading.Lock
        assert not a1.acquire(blocking=False)


def test_remove_repo(tmp_path: Path) -> None:
    s = make_settings(tmp_path)
    handles = open_code_index(s, "doomed", role="writer")
    handles.close()
    repo_dir = code_index_paths(s, "doomed").repo_dir
    assert repo_dir.exists()
    assert remove_repo(s, "doomed") is True
    assert not repo_dir.exists()
    assert remove_repo(s, "doomed") is False  # already gone


def test_open_jobs(tmp_path: Path) -> None:
    s = make_settings(tmp_path)
    jobs = open_jobs(s)
    try:
        assert code_index_paths(s, "any").jobs_path.exists()
        job = jobs.create_job(slug="r", repo_path="/r")
        assert jobs.get_job(job.job_id) is not None
    finally:
        jobs.close()
