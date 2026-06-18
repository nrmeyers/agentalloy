"""Unit tests for the DuckDB-backed dedup module.

No live LM Studio; ``embedder`` is injected via a tiny fake ``OpenAICompatClient``
stub that returns deterministic vectors. The ``VectorStore`` is a real DuckDB
instance in tmp_path — fast, isolated per test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentalloy.authoring.dedup import (
    classify_hit,
    dedup_candidates,
    dedup_fragment,
)
from agentalloy.storage.vector_store import (
    EMBEDDING_DIM,
    FragmentEmbedding,
    SimilarityHit,
    VectorStore,
    open_or_create,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unit_vec(i: int) -> list[float]:
    v = [0.0] * EMBEDDING_DIM
    v[i] = 1.0
    return v


def _mixed_vec(a: int, b: int, alpha: float) -> list[float]:
    """A vector that's ``alpha`` in dimension ``a`` and ``sqrt(1-alpha^2)``
    in dimension ``b`` — useful for hitting specific similarity levels."""
    import math

    v = [0.0] * EMBEDDING_DIM
    v[a] = alpha
    v[b] = math.sqrt(max(0.0, 1.0 - alpha * alpha))
    return v


class _FakeEmbedder:
    """Stub for OpenAICompatClient.embed. Maps content → a fixed vector via
    a caller-provided dict. Used in place of a real HTTP client."""

    def __init__(self, mapping: dict[str, list[float]]) -> None:
        self._mapping = mapping
        self.calls: list[list[str]] = []

    def embed(self, *, model: str, texts: list[str]) -> list[list[float]]:
        _ = model
        self.calls.append(texts)
        # dedup prefixes document text with the nomic ``search_document: `` tag;
        # strip it for the content→vector mapping lookup.
        prefix = "search_document: "
        return [self._mapping[t.removeprefix(prefix)] for t in texts]


@pytest.fixture
def store(tmp_path: Path):
    with open_or_create(tmp_path / "d.duck") as s:
        yield s


@pytest.fixture
def seeded_store(store: VectorStore):
    """Store pre-populated with 5 unit-vector fragments across 2 skills."""
    import time

    store.insert_embeddings(
        [
            FragmentEmbedding(
                fragment_id=f"existing-{i}",
                embedding=_unit_vec(i),
                skill_id="skill-a" if i < 3 else "skill-b",
                category="engineering",
                fragment_type="execution" if i % 2 == 0 else "guardrail",
                embedded_at=int(time.time()),
                embedding_model="nomic-embed-text-v1.5",
            )
            for i in range(5)
        ]
    )
    return store


# ---------------------------------------------------------------------------
# classify_hit
# ---------------------------------------------------------------------------


def test_classify_hit_identical_is_hard() -> None:
    hit = SimilarityHit(fragment_id="x", skill_id="s", distance=0.0)
    assert classify_hit(hit, hard_similarity=0.92, soft_similarity=0.80) == "hard"


def test_classify_hit_in_soft_band() -> None:
    # distance 0.15 → similarity 0.85 → soft band (0.80..0.92)
    hit = SimilarityHit(fragment_id="x", skill_id="s", distance=0.15)
    assert classify_hit(hit, hard_similarity=0.92, soft_similarity=0.80) == "soft"


def test_classify_hit_below_soft_is_ignore() -> None:
    # distance 0.5 → similarity 0.5 → below soft threshold
    hit = SimilarityHit(fragment_id="x", skill_id="s", distance=0.5)
    assert classify_hit(hit, hard_similarity=0.92, soft_similarity=0.80) == "ignore"


def test_classify_hit_boundary_hard() -> None:
    # distance 0.08 → similarity 0.92 → exactly on hard threshold (inclusive)
    hit = SimilarityHit(fragment_id="x", skill_id="s", distance=0.08)
    assert classify_hit(hit, hard_similarity=0.92, soft_similarity=0.80) == "hard"


def test_classify_hit_boundary_soft() -> None:
    # distance 0.2 → similarity 0.8 → exactly on soft threshold (inclusive)
    hit = SimilarityHit(fragment_id="x", skill_id="s", distance=0.2)
    assert classify_hit(hit, hard_similarity=0.92, soft_similarity=0.80) == "soft"


# ---------------------------------------------------------------------------
# dedup_fragment
# ---------------------------------------------------------------------------


def test_dedup_fragment_detects_identical_match(seeded_store: VectorStore) -> None:
    # Querying with the exact vector of existing-0 should produce a hard hit.
    result = dedup_fragment(
        label="frag-0",
        query_vec=_unit_vec(0),
        vector_store=seeded_store,
        hard_similarity=0.92,
        soft_similarity=0.80,
    )
    assert result.hard is not None
    assert result.hard.fragment_id == "existing-0"


def test_dedup_fragment_picks_hardest_match(seeded_store: VectorStore) -> None:
    """Multiple hard hits: return the one with smallest distance."""
    # Add a second fragment in dim 0 with a slight perturbation.
    import time

    from agentalloy.storage.vector_store import FragmentEmbedding

    seeded_store.insert_embeddings(
        [
            FragmentEmbedding(
                fragment_id="existing-0-dup",
                embedding=_mixed_vec(0, 1, 0.999),  # very close to unit_vec(0)
                skill_id="skill-c",
                category="engineering",
                fragment_type="execution",
                embedded_at=int(time.time()),
                embedding_model="test",
            )
        ]
    )
    result = dedup_fragment(
        label="q",
        query_vec=_unit_vec(0),
        vector_store=seeded_store,
        hard_similarity=0.92,
        soft_similarity=0.80,
    )
    # existing-0 is distance 0; existing-0-dup is ~0.001. Exact wins.
    assert result.hard is not None
    assert result.hard.fragment_id == "existing-0"


def test_dedup_fragment_only_soft_matches(seeded_store: VectorStore) -> None:
    """Query with a vector that's similarity ~0.85 to existing-0."""
    import math

    # similarity 0.85 = distance 0.15
    alpha = 0.85
    query = [0.0] * EMBEDDING_DIM
    query[0] = alpha
    query[99] = math.sqrt(1.0 - alpha * alpha)

    result = dedup_fragment(
        label="q",
        query_vec=query,
        vector_store=seeded_store,
        hard_similarity=0.92,
        soft_similarity=0.80,
    )
    assert result.hard is None
    assert any(h.fragment_id == "existing-0" for h in result.soft)


def test_dedup_fragment_no_matches(seeded_store: VectorStore) -> None:
    """Query with a vector orthogonal to every seed (similarity 0)."""
    result = dedup_fragment(
        label="q",
        query_vec=_unit_vec(200),  # no seed uses dim 200
        vector_store=seeded_store,
        hard_similarity=0.92,
        soft_similarity=0.80,
    )
    assert result.hard is None
    assert result.soft == []


def test_dedup_fragment_respects_fragment_type_filter(seeded_store: VectorStore) -> None:
    """Narrowing by fragment_type should only return matches of that type."""
    result = dedup_fragment(
        label="q",
        query_vec=_unit_vec(0),
        vector_store=seeded_store,
        hard_similarity=0.92,
        soft_similarity=0.80,
        fragment_types=["guardrail"],  # existing-0 is 'execution', should be filtered out
    )
    # The hard match (existing-0) is filtered out; no guardrail type matches dim 0 closely.
    assert result.hard is None


# ---------------------------------------------------------------------------
# dedup_candidates
# ---------------------------------------------------------------------------


def test_dedup_candidates_empty_input_skips_embedding(seeded_store: VectorStore) -> None:
    embedder = _FakeEmbedder({})
    result = dedup_candidates(
        labeled_contents=[],
        embedder=embedder,  # pyright: ignore[reportArgumentType]
        vector_store=seeded_store,
        embedding_model="test",
        hard_similarity=0.92,
        soft_similarity=0.80,
    )
    assert result.per_fragment == []
    assert result.hardest is None
    assert result.soft_all == []
    assert embedder.calls == []


def test_dedup_candidates_batches_embeddings_in_one_call(seeded_store: VectorStore) -> None:
    embedder = _FakeEmbedder(
        {
            "content-0": _unit_vec(0),
            "content-1": _unit_vec(1),
            "content-2": _unit_vec(2),
        }
    )
    result = dedup_candidates(
        labeled_contents=[
            ("frag-0", "content-0"),
            ("frag-1", "content-1"),
            ("frag-2", "content-2"),
        ],
        embedder=embedder,  # pyright: ignore[reportArgumentType]
        vector_store=seeded_store,
        embedding_model="test",
        hard_similarity=0.92,
        soft_similarity=0.80,
    )
    # Exactly one HTTP call for all three fragments.
    assert len(embedder.calls) == 1
    assert embedder.calls[0] == [
        "search_document: content-0",
        "search_document: content-1",
        "search_document: content-2",
    ]
    # Each candidate has a hard match against its corresponding seeded fragment.
    assert len(result.per_fragment) == 3
    assert all(pf.hard is not None for pf in result.per_fragment)


def test_dedup_candidates_hardest_is_min_distance_across_fragments(
    seeded_store: VectorStore,
) -> None:
    """When multiple candidates match, ``hardest`` is the overall smallest distance."""
    embedder = _FakeEmbedder(
        {
            "a": _unit_vec(0),  # distance 0 to existing-0
            "b": _mixed_vec(1, 50, 0.999),  # distance ~0.001 to existing-1
        }
    )
    result = dedup_candidates(
        labeled_contents=[("frag-a", "a"), ("frag-b", "b")],
        embedder=embedder,  # pyright: ignore[reportArgumentType]
        vector_store=seeded_store,
        embedding_model="test",
        hard_similarity=0.92,
        soft_similarity=0.80,
    )
    assert result.hardest is not None
    assert result.hardest.fragment_id == "existing-0"  # exact match wins


def test_dedup_candidates_deduplicates_soft_by_fragment_id(seeded_store: VectorStore) -> None:
    """If two candidate fragments both flag the same existing fragment as
    a soft match, the dedupe result's ``soft_all`` lists it once."""
    import math

    # Both a and b point to existing-0 with similarity ~0.85
    alpha = 0.85
    vec_a = [0.0] * EMBEDDING_DIM
    vec_a[0] = alpha
    vec_a[99] = math.sqrt(1.0 - alpha * alpha)
    vec_b = [0.0] * EMBEDDING_DIM
    vec_b[0] = alpha
    vec_b[100] = math.sqrt(1.0 - alpha * alpha)

    embedder = _FakeEmbedder({"a": vec_a, "b": vec_b})
    result = dedup_candidates(
        labeled_contents=[("frag-a", "a"), ("frag-b", "b")],
        embedder=embedder,  # pyright: ignore[reportArgumentType]
        vector_store=seeded_store,
        embedding_model="test",
        hard_similarity=0.92,
        soft_similarity=0.80,
    )
    # existing-0 shows up as a soft match for both; dedupe collapses to one.
    matching_existing_0 = [h for h in result.soft_all if h.fragment_id == "existing-0"]
    assert len(matching_existing_0) == 1


def test_dedup_candidates_no_duplicates_in_corpus_empty_result(tmp_path: Path) -> None:
    """Fresh store with no seeded embeddings: every candidate gets clean pass."""
    with open_or_create(tmp_path / "empty.duck") as empty_store:
        embedder = _FakeEmbedder({"c": _unit_vec(0)})
        result = dedup_candidates(
            labeled_contents=[("frag", "c")],
            embedder=embedder,  # pyright: ignore[reportArgumentType]
            vector_store=empty_store,
            embedding_model="test",
            hard_similarity=0.92,
            soft_similarity=0.80,
        )
    assert result.hardest is None
    assert result.soft_all == []
    assert result.per_fragment[0].hard is None
    assert result.per_fragment[0].soft == []
