"""Shared fixtures for the latency benchmarks.

Provides a minimal repo with a DuckDB-backed state store pre-loaded with
phase data. The legacy ``.agentalloy/phase`` file is no longer read by the
signal layer — these benchmarks measure store latency only.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    """A minimal repo with a DuckDB state store pre-loaded with phase data.

    The legacy ``.agentalloy/phase`` file is no longer the source of truth;
    the store is. This fixture exists for backward compatibility with
    benchmarks that expect a ``fixture_repo``.
    """
    return _build_store_repo(tmp_path / "bench_repo")


# ---------------------------------------------------------------------------
# store_repo – a repo + DuckDB store with the same phase data
# ---------------------------------------------------------------------------


@pytest.fixture
def store_repo(tmp_path: Path) -> Path:
    """A minimal repo with a DuckDB state store pre-loaded with phase data.

    Returns the repo path.  The caller should open the store at
    ``tmp_path / "sdd_state.duckdb"`` via :func:`DuckDBStateStore`.
    """
    return _build_store_repo(tmp_path / "bench_repo_store")


def _build_store_repo(repo: Path) -> Path:
    """Create a repo with a DuckDB state store containing a phase row."""
    repo.mkdir(parents=True, exist_ok=True)
    agentalloy_dir = repo / ".agentalloy"
    agentalloy_dir.mkdir(parents=True, exist_ok=True)
    agentalloy_dir.joinpath("pyproject.toml").write_text("[project]\nname = 'demo'\n")
    repo.joinpath("main.py").write_text("# demo\n")

    # Create a DuckDB store at a known location and seed it.
    db_path = repo.parent / (repo.name + ".duckdb")
    import duckdb  # noqa: PLC0415

    conn = duckdb.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sdd_state ("
            "repo TEXT NOT NULL, stream_id TEXT NOT NULL DEFAULT '', "
            "kind TEXT NOT NULL, session_key TEXT, "
            "value TEXT NOT NULL, owner TEXT, "
            "updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "lease_expires_at TIMESTAMP"
            ")"
        )
        conn.execute(
            "INSERT INTO sdd_state (repo, stream_id, kind, value) VALUES (?, '', 'phase', ?)",
            [repo.name, "build"],
        )
    finally:
        conn.close()
    # Also copy the db into the repo dir so benchmarks can find it.
    repo_db = repo / (repo.name + ".duckdb")
    shutil.copy2(db_path, repo_db)
    return repo
