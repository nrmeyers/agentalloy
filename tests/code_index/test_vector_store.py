"""OverGraphCodeGraphStore — merge semantics, atomic replace, search, dim guard."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from agentalloy.code_index.store.overgraph_store import OverGraphCodeGraphStore
from agentalloy.storage.protocols import (
    EMBEDDING_DIM,
    CodeVectorRow,
    CodeVectorStore,
    EmbeddingDimMismatch,
)


def vec(i: int, j: int | None = None) -> list[float]:
    """A hand-built normalized 768-dim vector: one-hot at ``i``, or an equal
    mix of axes ``i`` and ``j`` (cosine ~0.707 against either one-hot)."""
    v = [0.0] * EMBEDDING_DIM
    if j is None:
        v[i] = 1.0
    else:
        v[i] = v[j] = 1.0 / math.sqrt(2.0)
    return v


def row(
    qn: str, embedding: list[float], *, text: str = "", symbol_type: str = "Function"
) -> CodeVectorRow:
    return CodeVectorRow(
        qualified_name=qn,
        embedding=embedding,
        symbol_type=symbol_type,
        file_path=f"{qn.replace('.', '/')}.py",
        start_line=1,
        end_line=9,
        text=text or f"def {qn.rsplit('.', 1)[-1]}(): pass",
        indexed_at=1_700_000_000,
    )


@pytest.fixture
def store(tmp_path: Path) -> OverGraphCodeGraphStore:
    return OverGraphCodeGraphStore(tmp_path / "graph.overgraph")


def test_satisfies_protocol(store: OverGraphCodeGraphStore) -> None:
    assert isinstance(store, CodeVectorStore)


def test_upsert_and_count(store: OverGraphCodeGraphStore) -> None:
    assert store.count() == 0
    assert store.embedding_dim() is None  # empty dataset contract
    n = store.upsert([row("m.a", vec(0)), row("m.b", vec(1))])
    assert n == 2
    assert store.count() == 2
    assert store.embedding_dim() == EMBEDDING_DIM
    assert store.upsert([]) == 0


def test_upsert_merges_on_qualified_name(store: OverGraphCodeGraphStore) -> None:
    store.upsert([row("m.a", vec(0))])
    store.upsert([row("m.a", vec(1))])  # re-embed same symbol
    assert store.count() == 1  # replaced, not duplicated
    hits = store.search_similar(vec(1), k=1)
    assert hits[0].qualified_name == "m.a"
    assert hits[0].score == pytest.approx(1.0, abs=1e-5)


def test_bulk_replace_atomicity(store: OverGraphCodeGraphStore) -> None:
    store.upsert([row("old.a", vec(0)), row("old.b", vec(1))])
    n = store.bulk_replace([row("new.c", vec(2))])
    assert n == 1
    assert store.count() == 1  # old rows gone
    hits = store.search_similar(vec(2), k=10)
    assert [h.qualified_name for h in hits] == ["new.c"]
    # Replace-with-empty wipes the dataset.
    assert store.bulk_replace([]) == 0
    assert store.count() == 0


def test_dim_mismatch_raises(store: OverGraphCodeGraphStore) -> None:
    bad = row("m.a", [1.0, 0.0, 0.0])
    with pytest.raises(EmbeddingDimMismatch):
        store.upsert([bad])
    with pytest.raises(EmbeddingDimMismatch):
        store.bulk_replace([bad])
    with pytest.raises(EmbeddingDimMismatch):
        store.search_similar([1.0] * 5)


def test_search_similar_ordering(store: OverGraphCodeGraphStore) -> None:
    store.upsert(
        [
            row("hit.exact", vec(0)),  # cosine 1.0 vs query
            row("hit.partial", vec(0, 1)),  # cosine ~0.707
            row("hit.orthogonal", vec(1)),  # cosine 0.0
        ]
    )
    hits = store.search_similar(vec(0), k=3)
    assert [h.qualified_name for h in hits] == ["hit.exact", "hit.partial", "hit.orthogonal"]
    assert hits[0].score == pytest.approx(1.0, abs=1e-5)
    assert hits[1].score == pytest.approx(1.0 / math.sqrt(2.0), abs=1e-4)
    assert hits[2].score == pytest.approx(0.0, abs=1e-5)
    assert hits[0].file_path == "hit/exact.py"
    assert hits[0].start_line == 1 and hits[0].end_line == 9
    assert store.search_similar(vec(0), k=2) == hits[:2]


def test_search_similar_empty_dataset(store: OverGraphCodeGraphStore) -> None:
    assert store.search_similar(vec(0)) == []


def test_search_bm25(store: OverGraphCodeGraphStore) -> None:
    store.upsert(
        [
            row("m.frob", vec(0), text="def frobnicate(widget): return widget.spin()"),
            row("m.other", vec(1), text="def unrelated(): pass"),
        ]
    )
    # Unified store: the keyword leg is served off the Symbol nodes directly,
    # so it works as soon as rows are upserted; rebuild_fts_index() is a
    # protocol no-op kept for call-site compatibility.
    store.rebuild_fts_index()
    hits = store.search_bm25("frobnicate")
    assert [qn for qn, _ in hits] == ["m.frob"]
    assert hits[0][1] > 0.0
    assert store.search_bm25("   ") == []


def test_delete(store: OverGraphCodeGraphStore) -> None:
    store.upsert([row("m.a", vec(0)), row("m.b", vec(1)), row("m.c", vec(2))])
    assert store.delete(["m.a", "m.c", "m.missing"]) == 2
    assert store.count() == 1
    assert store.delete([]) == 0
