"""Unit tests for the runtime dedup gate (``agentalloy.dedup_gate``).

Deterministic unit vectors drive ``classify_hit`` and ``dedup_fragment`` against a
real unified OverGraph corpus store in tmp_path — fast, isolated per test, no network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentalloy.dedup_gate import (
    classify_hit,
    dedup_fragment,
)
from agentalloy.storage.overgraph_skill_store import OverGraphSkillStore, open_overgraph_skill_store
from agentalloy.storage.protocols import (
    EMBEDDING_DIM,
    FragmentEmbedding,
    SimilarityHit,
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


@pytest.fixture
def store(tmp_path: Path):
    s = open_overgraph_skill_store(str(tmp_path / "corpus.overgraph"))
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def seeded_store(store: OverGraphSkillStore):
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


def test_dedup_fragment_detects_identical_match(seeded_store: OverGraphSkillStore) -> None:
    # Querying with the exact vector of existing-0 should produce a hard hit.
    hard, soft = dedup_fragment(
        label="frag-0",
        query_vec=_unit_vec(0),
        vector_store=seeded_store,
        hard_similarity=0.92,
        soft_similarity=0.80,
    )
    assert hard is not None
    assert hard.fragment_id == "existing-0"


def test_dedup_fragment_picks_hardest_match(seeded_store: OverGraphSkillStore) -> None:
    """Multiple hard hits: return the one with smallest distance."""
    # Add a second fragment in dim 0 with a slight perturbation.
    import time

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
    hard, soft = dedup_fragment(
        label="q",
        query_vec=_unit_vec(0),
        vector_store=seeded_store,
        hard_similarity=0.92,
        soft_similarity=0.80,
    )
    # existing-0 is distance 0; existing-0-dup is ~0.001. Exact wins.
    assert hard is not None
    assert hard.fragment_id == "existing-0"


def test_dedup_fragment_only_soft_matches(seeded_store: OverGraphSkillStore) -> None:
    """Query with a vector that's similarity ~0.85 to existing-0."""
    import math

    # similarity 0.85 = distance 0.15
    alpha = 0.85
    query = [0.0] * EMBEDDING_DIM
    query[0] = alpha
    query[99] = math.sqrt(1.0 - alpha * alpha)

    hard, soft = dedup_fragment(
        label="q",
        query_vec=query,
        vector_store=seeded_store,
        hard_similarity=0.92,
        soft_similarity=0.80,
    )
    assert hard is None
    assert any(h.fragment_id == "existing-0" for h in soft)


def test_dedup_fragment_no_matches(seeded_store: OverGraphSkillStore) -> None:
    """Query with a vector orthogonal to every seed (similarity 0)."""
    hard, soft = dedup_fragment(
        label="q",
        query_vec=_unit_vec(200),  # no seed uses dim 200
        vector_store=seeded_store,
        hard_similarity=0.92,
        soft_similarity=0.80,
    )
    assert hard is None
    assert soft == []


def test_dedup_fragment_respects_fragment_type_filter(seeded_store: OverGraphSkillStore) -> None:
    """Narrowing by fragment_type should only return matches of that type."""
    hard, soft = dedup_fragment(
        label="q",
        query_vec=_unit_vec(0),
        vector_store=seeded_store,
        hard_similarity=0.92,
        soft_similarity=0.80,
        fragment_types=["guardrail"],  # existing-0 is 'execution', should be filtered out
    )
    # The hard match (existing-0) is filtered out; no guardrail type matches dim 0 closely.
    assert hard is None
