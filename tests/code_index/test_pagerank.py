"""Pure power-iteration PageRank + refresh_centrality persistence."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentalloy.code_index.store.graph_store import DuckDBCodeGraphStore
from agentalloy.code_index.store.pagerank import compute_pagerank, refresh_centrality
from agentalloy.storage.protocols import CodeEdge, CodeSymbol


def test_empty_graph() -> None:
    assert compute_pagerank([]) == {}


def test_known_small_graph_ordering() -> None:
    # a and b both call c; c calls d. Mass concentrates downstream: d > c > a == b.
    scores = compute_pagerank([("a", "c"), ("b", "c"), ("c", "d")])
    assert set(scores) == {"a", "b", "c", "d"}
    assert scores["d"] > scores["c"] > scores["a"]
    assert scores["a"] == pytest.approx(scores["b"])
    assert sum(scores.values()) == pytest.approx(1.0, abs=1e-6)


def test_cycle_terminates_with_symmetric_scores() -> None:
    scores = compute_pagerank([("a", "b"), ("b", "a")])
    assert scores["a"] == pytest.approx(0.5, abs=1e-6)
    assert scores["b"] == pytest.approx(0.5, abs=1e-6)


def test_duplicate_edges_are_collapsed() -> None:
    once = compute_pagerank([("a", "b"), ("a", "c")])
    dup = compute_pagerank([("a", "b"), ("a", "b"), ("a", "b"), ("a", "c")])
    for qn in once:
        assert dup[qn] == pytest.approx(once[qn], abs=1e-9)


def test_dangling_nodes_get_scores() -> None:
    # b has no out-edges (dangling); its mass redistributes instead of leaking.
    scores = compute_pagerank([("a", "b")])
    assert scores["b"] > scores["a"] > 0.0
    assert sum(scores.values()) == pytest.approx(1.0, abs=1e-6)


def test_refresh_centrality_writes_to_graph(tmp_path: Path) -> None:
    graph = DuckDBCodeGraphStore(tmp_path / "graph.duck")
    graph.migrate()
    try:
        graph.upsert_symbols(
            [
                CodeSymbol(
                    qualified_name=qn,
                    kind="Function",
                    name=qn,
                    file_path="m.py",
                    start_line=1,
                    end_line=1,
                    docstring=None,
                    decorators=[],
                    is_exported=None,
                    is_async=False,
                    is_generator=False,
                    source_code=None,
                )
                for qn in ("a", "b", "c")
            ]
        )
        graph.upsert_edges(
            [
                CodeEdge(src="a", dst="c", kind="CALLS"),
                CodeEdge(src="b", dst="c", kind="CALLS"),
                CodeEdge(src="a", dst="b", kind="CONTAINS"),  # non-CALLS ignored
            ]
        )
        written = refresh_centrality(graph)
        assert written == 3
        top = graph.top_centrality(limit=1)
        assert top[0][0] == "c"
        stored = graph.read_centrality(["a", "b", "c"])
        assert stored["c"] > stored["a"] == pytest.approx(stored["b"])
    finally:
        graph.close()


def test_refresh_centrality_empty_graph_clears(tmp_path: Path) -> None:
    graph = DuckDBCodeGraphStore(tmp_path / "graph.duck")
    graph.migrate()
    try:
        graph.write_centrality({"stale": 0.9})
        assert refresh_centrality(graph) == 0
        assert graph.top_centrality() == []
    finally:
        graph.close()
