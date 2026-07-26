"""Latency benchmarks for the state store.

Compares the file-backed phase read path against the DuckDB-backed
store path to verify that the store-backed path adds less than 5 ms
of overhead per request (AC-7).

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

from agentalloy.signals import skill_loader
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


def test_store_overhead(fixture_repo: Path, store_repo: Path) -> None:
    """Store-backed read path must add < 5 ms overhead over file-backed reads.

    Measures two paths:
    1. **File-backed** — ``skill_loader._read_phase()`` (YAML parse + file I/O).
    2. **Store-backed** — ``DuckDBStateStore.read("phase")`` (SQL query).

    The overhead is the difference between the two.
    """
    # --- file-backed path ---
    file_latency = _measure_latency(lambda: skill_loader._read_phase(fixture_repo))

    # --- store-backed path ---
    # Open a read-only DuckDB handle on the store repo's db file.
    db_path = store_repo / "bench_repo_store.duckdb"
    assert db_path.exists(), "store_repo fixture should have created the DuckDB file"

    store = DuckDBStateStore(db_path, read_only=True).open()
    try:
        store_latency = _measure_latency(lambda: store.read("phase"))
    finally:
        store.close()

    overhead = store_latency - file_latency
    assert overhead < 5.0, (
        f"Store overhead {overhead:.2f} ms exceeds the 5 ms budget "
        f"(file={file_latency:.2f} ms, store={store_latency:.2f} ms)"
    )


# ---------------------------------------------------------------------------
# Absolute latencies (for documentation / trend tracking)
# ---------------------------------------------------------------------------


def test_file_backed_latency(fixture_repo: Path) -> None:
    """Document the absolute file-backed read latency."""
    avg_ms = _measure_latency(lambda: skill_loader._read_phase(fixture_repo))
    # Sanity: file-backed should be fast (< 10 ms even on cold cache).
    assert avg_ms < 10.0, f"File-backed read took {avg_ms:.2f} ms — unexpected slowness"


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
