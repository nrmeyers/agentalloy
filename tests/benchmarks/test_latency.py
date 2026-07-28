"""Latency benchmarks for the state store.

Measures the DuckDB-backed store read path to verify it adds less than 5 ms
of overhead per request (AC-7). The file-backed phase read path has been
removed — the store is the sole source of truth.

Run the full suite::

    uv run pytest tests/benchmarks/test_latency.py -v

Or just the overhead check::

    uv run pytest tests/benchmarks/test_latency.py::test_store_overhead -v

The benchmark is self-contained and repeatable — no external services,
no agent model, no network — making it safe to run in CI.
"""

from __future__ import annotations

import time
from pathlib import Path

from agentalloy.storage.state_store import DuckDBStateStore

# ---------------------------------------------------------------------------
# Timing helpers
# ---------------------------------------------------------------------------

_ITERATIONS = 200  # enough to smooth OS jitter while keeping each test < 2 s


def _measure_latency(fn, iterations: int = _ITERATIONS) -> float:
    """Return the average latency of *fn* in milliseconds."""
    times_ms: list[float] = []
    for _ in range(iterations):
        start = time.perf_counter_ns()
        fn()
        end = time.perf_counter_ns()
        times_ms.append((end - start) / 1_000_000)
    return sum(times_ms) / len(times_ms)


# ---------------------------------------------------------------------------
# AC-7: store-backed path adds < 5 ms overhead per request
# ---------------------------------------------------------------------------


def test_store_overhead(store_repo: Path) -> None:
    """Store-backed read path must add < 5 ms overhead per request.

    Measures ``DuckDBStateStore.read("phase")`` (SQL query) and verifies
    it stays under the 5 ms budget.
    """
    # Open a read-only DuckDB handle on the store repo's db file.
    db_path = store_repo / "bench_repo_store.duckdb"
    assert db_path.exists(), "store_repo fixture should have created the DuckDB file"

    store = DuckDBStateStore(db_path, read_only=True).open()
    try:
        store_latency = _measure_latency(lambda: store.read("phase"))
    finally:
        store.close()

    assert store_latency < 5.0, f"Store read latency {store_latency:.2f} ms exceeds the 5 ms budget"


# ---------------------------------------------------------------------------
# Absolute latencies (for documentation / trend tracking)
# ---------------------------------------------------------------------------


def test_store_backed_latency(store_repo: Path) -> None:
    """Document the absolute store-backed read latency."""
    db_path = store_repo / "bench_repo_store.duckdb"
    store = DuckDBStateStore(db_path, read_only=True).open()
    try:
        avg_ms = _measure_latency(lambda: store.read("phase"))
        # Store-backed should be under 50 ms (DuckDB cold open + query).
        assert avg_ms < 50.0, f"Store-backed read took {avg_ms:.2f} ms — unexpected slowness"
    finally:
        store.close()
