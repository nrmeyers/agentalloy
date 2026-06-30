"""Unit tests for the LanceDB FragmentStore (v5 storage layer).

NOTE: these are self-contained (own tmp Lance dataset); they do not use the
``conftest`` corpus fixtures, which are rewired to the two-engine backend in the
test-porting step.
"""

from __future__ import annotations

import pytest

from agentalloy.storage import EMBEDDING_DIM, EmbeddingDimMismatch, FragmentEmbedding
from agentalloy.storage.fragment_store import LanceFragmentStore

DIM = EMBEDDING_DIM


def _vec(seed: float) -> list[float]:
    return [(seed + 1.0)] + [seed * 0.001 + i * 1e-4 for i in range(DIM - 1)]


def _frag(i: int) -> FragmentEmbedding:
    return FragmentEmbedding(
        fragment_id=f"f{i}",
        embedding=_vec(float(i)),
        skill_id=f"s{i % 3}",
        category=["engineering", "ops", "review"][i % 3],
        fragment_type="card" if i % 2 else "execution",
        embedded_at=1000 + i,
        embedding_model="nomic-embed-text-v1.5",
        prose=f"fragment {i} about testing lance retrieval and duckdb",
        phase_scope=(["build"], ["design", "build"], None)[i % 3],
    )


@pytest.fixture
def store(tmp_path):
    fs = LanceFragmentStore(str(tmp_path / "fragments.lance"))
    yield fs
    fs.close()


def test_empty_corpus(store):
    assert store.embedding_dim() is None
    assert store.count_embeddings() == 0
    assert store.search_similar(_vec(1.0)) == []


def test_insert_and_dim(store):
    store.insert_embeddings([_frag(i) for i in range(6)])
    assert store.count_embeddings() == 6
    assert store.embedding_dim() == EMBEDDING_DIM  # row-count gated -> int when populated


def test_exact_cosine_search(store):
    store.insert_embeddings([_frag(i) for i in range(6)])
    hits = store.search_similar(_vec(2.0), k=3)
    assert hits[0].fragment_id == "f2"
    assert hits[0].distance < 1e-4  # exact: identical vector -> ~0 cosine distance


def test_filters(store):
    store.insert_embeddings([_frag(i) for i in range(6)])
    cards = store.search_similar(_vec(0.0), fragment_types=["card"], k=10)
    assert cards and all(True for _ in cards)
    dep = store.search_similar(_vec(0.0), deprecated_skill_ids=["s0"], k=10)
    assert all(h.skill_id != "s0" for h in dep)
    eng = store.search_similar(_vec(0.0), categories=["engineering"], k=10)
    assert len(eng) >= 1


def test_bm25(store):
    store.insert_embeddings([_frag(i) for i in range(6)])
    store.rebuild_fts_index()
    assert len(store.search_bm25("testing retrieval", k=5)) >= 1
    assert store.search_bm25("   ") == []


def test_dim_mismatch_raises_with_marker(store):
    with pytest.raises(EmbeddingDimMismatch) as ei:
        store.insert_embeddings(
            [
                FragmentEmbedding(
                    fragment_id="bad",
                    embedding=[0.1] * 10,
                    skill_id="sx",
                    category="ops",
                    fragment_type="execution",
                    embedded_at=1,
                    embedding_model="x",
                    prose="bad",
                )
            ]
        )
    # upgrade.py greps stderr for these substrings to trigger self-heal re-embed.
    assert "dimension" in str(ei.value).lower()


def test_presence_counts_deletes(store):
    store.insert_embeddings([_frag(i) for i in range(6)])
    assert store.fragment_ids_present(["f0", "f1", "nope"]) == {"f0", "f1"}
    assert store.count_cards() >= 1
    n = store.delete_skill("s0")
    assert n >= 1
    assert store.count_embeddings() == 6 - n


def test_bulk_replace_and_delete_all(store):
    store.insert_embeddings([_frag(i) for i in range(6)])
    assert store.bulk_replace([_frag(i) for i in range(4)]) == 4
    assert store.count_embeddings() == 4  # atomic full replace
    assert store.delete_all() == 4
    assert store.count_embeddings() == 0
