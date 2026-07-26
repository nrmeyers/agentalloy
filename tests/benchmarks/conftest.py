"""Shared fixtures for the latency benchmarks.

Provides a minimal repo with a phase file (``fixture_repo``) and a
DuckDB-backed state store pre-loaded with the same phase data
``(store_repo)`` so the file-backed and store-backed read paths can be
compared head-to-head.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# fixture_repo – minimal repo with a phase file
# ---------------------------------------------------------------------------


def _build_phase_file(path: Path) -> None:
    """Write a small ``.agentalloy/phase`` file."""
    agentalloy_dir = path / ".agentalloy"
    agentalloy_dir.mkdir(parents=True, exist_ok=True)
    agentalloy_dir.joinpath("pyproject.toml").write_text("[project]\nname = 'demo'\n")
    path.joinpath("main.py").write_text("# demo\n")
    phase_file = agentalloy_dir / "phase"
    phase_file.write_text("phase: build\nworkflow: SDD\n")


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    """A minimal repo with a ``.agentalloy/phase`` file for benchmarking."""
    repo = tmp_path / "bench_repo"
    _build_phase_file(repo)
    return repo


# ---------------------------------------------------------------------------
# store_repo – a repo + DuckDB store with the same phase data
# ---------------------------------------------------------------------------


@pytest.fixture
def store_repo(tmp_path: Path) -> Path:
    """A minimal repo with a DuckDB state store pre-loaded with phase data.

    Returns the repo path.  The caller should open the store at
    ``tmp_path / "sdd_state.duckdb"`` via :func:`DuckDBStateStore`.
    """
    repo = tmp_path / "bench_repo_store"
    _build_phase_file(repo)
    # Create a DuckDB store at a known location and seed it.
    db_path = tmp_path / "bench_repo_store.duckdb"
    import duckdb

    conn = duckdb.connect(str(db_path))
    try:
        # Create the table directly (bypassing migrate() which may include
        # partial indexes not supported by all DuckDB versions).
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sdd_state ("
            "repo TEXT NOT NULL, kind TEXT NOT NULL, session_key TEXT, "
            "value TEXT NOT NULL, owner TEXT, "
            "updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "lease_expires_at TIMESTAMP"
            ")"
        )
        conn.execute(
            "INSERT INTO sdd_state (repo, kind, value) VALUES (?, 'phase', ?)",
            ["bench_repo_store", "build"],
        )
    finally:
        conn.close()
    # Also copy the db into the repo dir so the benchmark can find it.
    repo_db = repo / "bench_repo_store.duckdb"
    shutil.copy2(db_path, repo_db)
    return repo
