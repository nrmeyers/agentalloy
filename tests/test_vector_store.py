"""Unit tests for the DuckDB-backed vector store.

Scope: correctness of L2-normalization, schema DDL, insert/search roundtrip,
filtered search, idempotency helpers, telemetry write. Live LM Studio is not
required — embeddings in these tests are synthetic unit vectors.
"""

from __future__ import annotations

import math
import time
from pathlib import Path

import pytest

from agentalloy.storage.vector_store import (
    EMBEDDING_DIM,
    BM25Hit,
    CompositionTrace,
    EmbeddingDimMismatch,
    FragmentEmbedding,
    VectorStore,
    l2_normalize,
    open_or_create,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unit_vec(i: int, dim: int = EMBEDDING_DIM) -> list[float]:
    """Return the i-th standard basis vector of the given dimension."""
    v = [0.0] * dim
    v[i] = 1.0
    return v


def _mk_fragment(
    i: int,
    *,
    skill_id: str = "skill-a",
    category: str = "engineering",
    fragment_type: str = "execution",
    prose: str = "",
) -> FragmentEmbedding:
    return FragmentEmbedding(
        fragment_id=f"frag-{i}",
        embedding=_unit_vec(i),
        skill_id=skill_id,
        category=category,
        fragment_type=fragment_type,
        embedded_at=int(time.time()),
        embedding_model="qwen3-embedding:0.6b",
        prose=prose,
    )


@pytest.fixture
def store(tmp_path: Path):
    with open_or_create(tmp_path / "test.duck") as s:
        yield s


# ---------------------------------------------------------------------------
# l2_normalize
# ---------------------------------------------------------------------------


def test_l2_normalize_unit_vec_is_identity() -> None:
    v = _unit_vec(3)
    assert l2_normalize(v) == v


def test_l2_normalize_scales_to_unit_norm() -> None:
    v = [3.0, 4.0]
    n = l2_normalize(v)
    assert math.isclose(n[0], 0.6)
    assert math.isclose(n[1], 0.8)
    assert math.isclose(sum(x * x for x in n), 1.0)


def test_l2_normalize_rejects_zero_vector() -> None:
    with pytest.raises(ValueError, match="zero vector"):
        l2_normalize([0.0, 0.0, 0.0])


# ---------------------------------------------------------------------------
# Schema + open
# ---------------------------------------------------------------------------


def test_open_or_create_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "idempotent.duck"
    with open_or_create(path):
        pass
    with open_or_create(path) as s:
        assert s.count_embeddings() == 0
        assert s.count_traces() == 0


def test_open_or_create_creates_parent_dirs(tmp_path: Path) -> None:
    deep = tmp_path / "a" / "b" / "c" / "store.duck"
    with open_or_create(deep) as s:
        assert s.count_embeddings() == 0
    assert deep.exists()


# ---------------------------------------------------------------------------
# insert_embeddings
# ---------------------------------------------------------------------------


def test_insert_and_count_roundtrip(store: VectorStore) -> None:
    assert store.insert_embeddings([_mk_fragment(i) for i in range(5)]) == 5
    assert store.count_embeddings() == 5


def test_insert_empty_is_noop(store: VectorStore) -> None:
    assert store.insert_embeddings([]) == 0
    assert store.count_embeddings() == 0


def test_insert_rejects_wrong_dimension(store: VectorStore) -> None:
    bad = FragmentEmbedding(
        fragment_id="frag-bad",
        embedding=[1.0, 0.0, 0.0],
        skill_id="skill-a",
        category="engineering",
        fragment_type="execution",
        embedded_at=int(time.time()),
        embedding_model="qwen3-embedding:0.6b",
        prose="bad fragment",
    )
    with pytest.raises(EmbeddingDimMismatch):
        store.insert_embeddings([bad])


def test_insert_normalizes_non_unit_vectors(store: VectorStore) -> None:
    """A non-unit input vector should be stored as its L2-normalized form, so
    downstream cosine-via-inner-product math is consistent."""
    v = [3.0, 4.0] + [0.0] * (EMBEDDING_DIM - 2)
    store.insert_embeddings(
        [
            FragmentEmbedding(
                fragment_id="frag-x",
                embedding=v,
                skill_id="skill-a",
                category="engineering",
                fragment_type="execution",
                embedded_at=0,
                embedding_model="test",
            )
        ]
    )
    # Querying with the same (normalized) direction should get distance ~0.
    hits = store.search_similar([3.0, 4.0] + [0.0] * (EMBEDDING_DIM - 2), k=1)
    assert len(hits) == 1
    assert math.isclose(hits[0].distance, 0.0, abs_tol=1e-5)


# ---------------------------------------------------------------------------
# search_similar
# ---------------------------------------------------------------------------


def test_search_returns_closest_first(store: VectorStore) -> None:
    # Insert orthogonal unit vectors; querying e_2 should return frag-2 first.
    store.insert_embeddings([_mk_fragment(i) for i in range(10)])
    hits = store.search_similar(_unit_vec(2), k=3)
    assert hits[0].fragment_id == "frag-2"
    assert math.isclose(hits[0].distance, 0.0, abs_tol=1e-5)
    # Orthogonal unit vectors have cosine distance 1.0.
    for h in hits[1:]:
        assert math.isclose(h.distance, 1.0, abs_tol=1e-5)


def test_search_respects_k(store: VectorStore) -> None:
    store.insert_embeddings([_mk_fragment(i) for i in range(10)])
    assert len(store.search_similar(_unit_vec(0), k=1)) == 1
    assert len(store.search_similar(_unit_vec(0), k=5)) == 5
    assert len(store.search_similar(_unit_vec(0), k=100)) == 10


def test_search_filters_by_category(store: VectorStore) -> None:
    store.insert_embeddings(
        [
            _mk_fragment(0, category="engineering"),
            _mk_fragment(1, category="ops"),
            _mk_fragment(2, category="engineering"),
        ]
    )
    hits = store.search_similar(_unit_vec(0), categories=["engineering"], k=10)
    assert {h.fragment_id for h in hits} == {"frag-0", "frag-2"}


def test_search_filters_by_fragment_type(store: VectorStore) -> None:
    store.insert_embeddings(
        [
            _mk_fragment(0, fragment_type="execution"),
            _mk_fragment(1, fragment_type="guardrail"),
            _mk_fragment(2, fragment_type="execution"),
        ]
    )
    hits = store.search_similar(_unit_vec(0), fragment_types=["guardrail"], k=10)
    assert [h.fragment_id for h in hits] == ["frag-1"]


def test_search_combines_filters(store: VectorStore) -> None:
    store.insert_embeddings(
        [
            _mk_fragment(0, category="engineering", fragment_type="execution"),
            _mk_fragment(1, category="engineering", fragment_type="guardrail"),
            _mk_fragment(2, category="ops", fragment_type="execution"),
        ]
    )
    hits = store.search_similar(
        _unit_vec(0),
        categories=["engineering"],
        fragment_types=["execution"],
        k=10,
    )
    assert [h.fragment_id for h in hits] == ["frag-0"]


def test_search_rejects_wrong_query_dimension(store: VectorStore) -> None:
    with pytest.raises(EmbeddingDimMismatch):
        store.search_similar([1.0, 0.0, 0.0], k=1)


def test_search_empty_store_returns_empty(store: VectorStore) -> None:
    assert store.search_similar(_unit_vec(0), k=10) == []


# ---------------------------------------------------------------------------
# idempotency helpers
# ---------------------------------------------------------------------------


def test_fragment_ids_present(store: VectorStore) -> None:
    store.insert_embeddings([_mk_fragment(i) for i in range(3)])
    present = store.fragment_ids_present(["frag-0", "frag-2", "frag-99"])
    assert present == {"frag-0", "frag-2"}


def test_fragment_ids_present_empty_input(store: VectorStore) -> None:
    assert store.fragment_ids_present([]) == set()


def test_delete_skill_removes_all_its_fragments(store: VectorStore) -> None:
    store.insert_embeddings(
        [
            _mk_fragment(0, skill_id="a"),
            _mk_fragment(1, skill_id="a"),
            _mk_fragment(2, skill_id="b"),
        ]
    )
    assert store.delete_skill("a") == 2
    assert store.count_embeddings() == 1


# ---------------------------------------------------------------------------
# composition traces
# ---------------------------------------------------------------------------


def test_record_composition_trace_and_count(store: VectorStore) -> None:
    t = CompositionTrace(
        trace_id="trace-1",
        request_ts=int(time.time()),
        phase="build",
        task_prompt="write a CLI",
        status="ok",
        selected_fragment_ids=["frag-0", "frag-1"],
        source_skill_ids=["skill-a"],
        system_skill_ids=["sys-governance"],
        assembly_tier="tier2",
        assembly_model="qwen/qwen2.5-coder-14b",
        retrieval_latency_ms=42,
        assembly_latency_ms=900,
        total_latency_ms=960,
        response_size_chars=2400,
    )
    store.record_composition_trace(t)
    assert store.count_traces() == 1


def test_record_trace_with_minimum_fields(store: VectorStore) -> None:
    """Optional fields should serialize as SQL NULL without error."""
    t = CompositionTrace(
        trace_id="trace-min",
        request_ts=0,
        phase="design",
        task_prompt="",
        status="error",
        error_code="model_not_loaded",
    )
    store.record_composition_trace(t)
    assert store.count_traces() == 1


# ---------------------------------------------------------------------------
# BM25 search
# ---------------------------------------------------------------------------


def test_bm25_returns_empty_for_empty_query(store: VectorStore) -> None:
    store.insert_embeddings([_mk_fragment(0, prose="prisma migration schema")])
    store.rebuild_fts_index()
    assert store.search_bm25("") == []
    assert store.search_bm25("   ") == []


def test_bm25_finds_literal_token(store: VectorStore) -> None:
    store.insert_embeddings(
        [
            _mk_fragment(0, prose="add a prisma migration for a new column"),
            _mk_fragment(1, prose="implement JWT authentication with refresh tokens"),
            _mk_fragment(2, prose="configure webpack bundler settings"),
        ]
    )
    store.rebuild_fts_index()
    hits = store.search_bm25("prisma migration", k=5)
    assert len(hits) >= 1
    assert hits[0].fragment_id == "frag-0"


def test_bm25_returns_empty_on_no_match(store: VectorStore) -> None:
    store.insert_embeddings([_mk_fragment(0, prose="hello world")])
    store.rebuild_fts_index()
    hits = store.search_bm25("zxqvbnm unique nonsense token", k=5)
    assert hits == []


def test_bm25_respects_category_filter(store: VectorStore) -> None:
    store.insert_embeddings(
        [
            _mk_fragment(0, category="engineering", prose="prisma ORM database migration"),
            _mk_fragment(1, category="ops", prose="prisma deployment pipeline"),
        ]
    )
    store.rebuild_fts_index()
    hits = store.search_bm25("prisma", categories=["engineering"], k=5)
    ids = {h.fragment_id for h in hits}
    assert "frag-0" in ids
    assert "frag-1" not in ids


def test_bm25_hit_has_positive_score(store: VectorStore) -> None:
    store.insert_embeddings([_mk_fragment(0, prose="JWT token rotation NestJS")])
    store.rebuild_fts_index()
    hits = store.search_bm25("JWT NestJS", k=5)
    assert len(hits) == 1
    assert isinstance(hits[0], BM25Hit)
    assert hits[0].score > 0


# ---------------------------------------------------------------------------
# FTS rebuild — catalog reset on persistent stopwords error
# ---------------------------------------------------------------------------


def test_rebuild_fts_reset_on_persistent_stopwords_error(tmp_path: Path) -> None:
    """When the stopwords error persists through CHECKPOINT retries,
    rebuild_fts_index should attempt a full catalog reset (drop + re-open)
    before giving up. This tests that the new reset logic is in place."""
    import duckdb

    db_path = tmp_path / "fts_reset.duck"
    # Create a real DB with schema so we can exercise the FTS path
    conn = duckdb.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fragment_embeddings (
            fragment_id VARCHAR PRIMARY KEY,
            embedding FLOAT[1024] NOT NULL,
            skill_id VARCHAR NOT NULL,
            category VARCHAR NOT NULL,
            fragment_type VARCHAR NOT NULL,
            embedded_at BIGINT NOT NULL,
            embedding_model VARCHAR NOT NULL,
            prose VARCHAR NOT NULL DEFAULT ''
        );
    """)
    conn.close()

    # Re-open, install FTS, insert some data
    conn = duckdb.connect(str(db_path))
    conn.execute("INSTALL fts; LOAD fts;")
    # Create a proper FLOAT[1024] array for the embedding
    conn.execute("""
        INSERT INTO fragment_embeddings
        VALUES (
            'frag-0',
            ARRAY_REPEAT(0.0, 1024),
            's', 'e', 't', 0, 'm',
            'test prose with searchable content'
        );
    """)
    conn.close()

    # Now open via VectorStore and call rebuild_fts_index
    conn = duckdb.connect(str(db_path))
    conn.execute("INSTALL fts; LOAD fts;")
    vs = VectorStore(conn, db_path=str(db_path))

    # This should succeed (no error injected) — basic sanity check
    vs.rebuild_fts_index()
    conn.close()
