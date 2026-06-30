"""Unit tests for the reembed-boundary dedup gate.

No embed server required — all embeddings are deterministic synthetic unit
vectors following the same pattern as tests/test_dedup.py and
tests/test_vector_store.py.

Coverage:
- Cross-pack hard duplicate → gate fires, correct skill_ids reported.
- Cross-pack hard dup + --allow-duplicates → warning only, EXIT_OK.
- Cross-pack soft duplicate → soft match recorded, no hard.
- Same-pack (same new_skill_ids set) → gate does NOT fire.
- Existing-corpus re-embed (no new skills) → gate does NOT fire.
- Direct unit tests for classify_hit / dedup_fragment helpers.
"""

from __future__ import annotations

import logging
import math
import time
from pathlib import Path

import pytest

from agentalloy.dedup_gate import (
    DedupGateResult,
    DedupMatch,
    classify_hit,
    dedup_fragment,
    run_dedup_gate,
)
from agentalloy.storage.fragment_store import LanceFragmentStore
from agentalloy.storage.protocols import (
    EMBEDDING_DIM,
    FragmentEmbedding,
    SimilarityHit,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HARD = 0.92
_SOFT = 0.80


def _unit_vec(i: int) -> list[float]:
    """i-th standard basis vector (L2 norm = 1)."""
    v = [0.0] * EMBEDDING_DIM
    v[i] = 1.0
    return v


def _mixed_vec(i: int, j: int, alpha: float) -> list[float]:
    """Vector with components at dims i and j, cosine-similarity = alpha to unit_vec(i)."""
    v = [0.0] * EMBEDDING_DIM
    v[i] = alpha
    v[j] = math.sqrt(max(0.0, 1.0 - alpha * alpha))
    return v


def _mk_fragment(
    fragment_id: str,
    *,
    skill_id: str,
    vec: list[float],
    category: str = "engineering",
    fragment_type: str = "execution",
) -> FragmentEmbedding:
    return FragmentEmbedding(
        fragment_id=fragment_id,
        embedding=vec,
        skill_id=skill_id,
        category=category,
        fragment_type=fragment_type,
        embedded_at=int(time.time()),
        embedding_model="test-model",
        prose="",
    )


@pytest.fixture
def store(tmp_path: Path):
    s = LanceFragmentStore(tmp_path / "fragments.lance")
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def seeded_store(store: LanceFragmentStore) -> LanceFragmentStore:
    """Store pre-seeded with two existing-corpus skills from different packs.

    pack-alpha: existing-skill-a  →  fragment existing-a-f1 (dim 0)
    pack-beta:  existing-skill-b  →  fragment existing-b-f1 (dim 1)
    """
    store.insert_embeddings(
        [
            _mk_fragment("existing-a-f1", skill_id="existing-skill-a", vec=_unit_vec(0)),
            _mk_fragment("existing-b-f1", skill_id="existing-skill-b", vec=_unit_vec(1)),
        ]
    )
    return store


# ---------------------------------------------------------------------------
# Unit tests: classify_hit
# ---------------------------------------------------------------------------


def test_classify_hit_hard() -> None:
    hit = SimilarityHit(fragment_id="x", skill_id="s", distance=0.05)  # similarity=0.95
    assert classify_hit(hit, hard_similarity=_HARD, soft_similarity=_SOFT) == "hard"


def test_classify_hit_soft() -> None:
    hit = SimilarityHit(fragment_id="x", skill_id="s", distance=0.15)  # similarity=0.85
    assert classify_hit(hit, hard_similarity=_HARD, soft_similarity=_SOFT) == "soft"


def test_classify_hit_ignore() -> None:
    hit = SimilarityHit(fragment_id="x", skill_id="s", distance=0.30)  # similarity=0.70
    assert classify_hit(hit, hard_similarity=_HARD, soft_similarity=_SOFT) == "ignore"


def test_classify_hit_boundary_hard() -> None:
    hit = SimilarityHit(fragment_id="x", skill_id="s", distance=1.0 - _HARD)
    assert classify_hit(hit, hard_similarity=_HARD, soft_similarity=_SOFT) == "hard"


def test_classify_hit_boundary_soft() -> None:
    hit = SimilarityHit(fragment_id="x", skill_id="s", distance=1.0 - _SOFT)
    assert classify_hit(hit, hard_similarity=_HARD, soft_similarity=_SOFT) == "soft"


# ---------------------------------------------------------------------------
# Unit tests: dedup_fragment helper
# ---------------------------------------------------------------------------


def test_dedup_fragment_hard_match(seeded_store: LanceFragmentStore) -> None:
    hard, soft = dedup_fragment(
        label="q",
        query_vec=_unit_vec(0),  # identical to existing-a-f1
        vector_store=seeded_store,
        hard_similarity=_HARD,
        soft_similarity=_SOFT,
    )
    assert hard is not None
    assert hard.fragment_id == "existing-a-f1"
    assert hard.skill_id == "existing-skill-a"
    assert soft == []


def test_dedup_fragment_soft_match(seeded_store: LanceFragmentStore) -> None:
    # similarity ≈ 0.85 (distance ≈ 0.15) to existing-a-f1
    query = _mixed_vec(0, 99, 0.85)
    hard, soft = dedup_fragment(
        label="q",
        query_vec=query,
        vector_store=seeded_store,
        hard_similarity=_HARD,
        soft_similarity=_SOFT,
    )
    assert hard is None
    assert any(h.fragment_id == "existing-a-f1" for h in soft)


def test_dedup_fragment_no_match(seeded_store: LanceFragmentStore) -> None:
    hard, soft = dedup_fragment(
        label="q",
        query_vec=_unit_vec(200),  # orthogonal — similarity 0
        vector_store=seeded_store,
        hard_similarity=_HARD,
        soft_similarity=_SOFT,
    )
    assert hard is None
    assert soft == []


# ---------------------------------------------------------------------------
# run_dedup_gate: cross-pack hard duplicate
# ---------------------------------------------------------------------------


def test_gate_hard_cross_pack(seeded_store: LanceFragmentStore) -> None:
    """New skill-from-pack-gamma nearly identical to existing-skill-a → hard."""
    new_frag_id = "new-gamma-f1"
    new_skill_id = "new-skill-gamma"

    # Insert the new skill so vector_store can return it in search_similar results
    # (the gate queries the store after insertion).
    seeded_store.insert_embeddings(
        [_mk_fragment(new_frag_id, skill_id=new_skill_id, vec=_unit_vec(0))]
    )

    result = run_dedup_gate(
        new_skill_ids={new_skill_id},
        new_fragment_vecs={new_frag_id: (new_skill_id, _unit_vec(0))},
        vector_store=seeded_store,
        hard_similarity=_HARD,
        soft_similarity=_SOFT,
    )

    assert result.has_hard
    hard_match = result.hard[0]
    assert hard_match.incoming_skill_id == new_skill_id
    assert hard_match.existing_skill_id == "existing-skill-a"
    assert hard_match.fragment_id_incoming == new_frag_id
    assert hard_match.verdict == "hard"
    assert hard_match.similarity >= _HARD


def test_gate_logs_start_and_done(
    seeded_store: LanceFragmentStore, caplog: pytest.LogCaptureFixture
) -> None:
    """The gate logs start + done so a full re-embed (thousands of fragments,
    one DuckDB vector search each) isn't a silent multi-minute hang."""
    new_skill_id = "new-skill-gamma"
    seeded_store.insert_embeddings(
        [_mk_fragment("new-gamma-f1", skill_id=new_skill_id, vec=_unit_vec(0))]
    )
    with caplog.at_level(logging.INFO, logger="agentalloy.dedup_gate"):
        run_dedup_gate(
            new_skill_ids={new_skill_id},
            new_fragment_vecs={"new-gamma-f1": (new_skill_id, _unit_vec(0))},
            vector_store=seeded_store,
            hard_similarity=_HARD,
            soft_similarity=_SOFT,
        )
    assert "dedup gate: scanning 1 new fragment" in caplog.text
    assert "dedup gate: done" in caplog.text


def test_gate_self_match_does_not_shadow_cross_pack_hard(seeded_store: LanceFragmentStore) -> None:
    """A ~0-distance self-match must not hide a slightly-farther cross-pack hard dup.

    The new fragment is a hard duplicate of existing-skill-a (cosine 0.95) but not
    identical, so after insertion its OWN row (distance 0) is the closest hit. The
    gate must still report the cross-pack hard match rather than picking the
    self-match as the single closest hard hit and then filtering it away.
    """
    new_frag_id = "new-eps-f1"
    new_skill_id = "new-skill-eps"
    vec = _mixed_vec(0, 5, 0.95)  # hard (≥ _HARD) vs existing-skill-a, not identical

    seeded_store.insert_embeddings([_mk_fragment(new_frag_id, skill_id=new_skill_id, vec=vec)])

    result = run_dedup_gate(
        new_skill_ids={new_skill_id},
        new_fragment_vecs={new_frag_id: (new_skill_id, vec)},
        vector_store=seeded_store,
        hard_similarity=_HARD,
        soft_similarity=_SOFT,
    )

    assert result.has_hard
    assert result.hard[0].existing_skill_id == "existing-skill-a"
    assert result.hard[0].incoming_skill_id == new_skill_id


def test_gate_hard_cross_pack_allow_duplicates(
    seeded_store: LanceFragmentStore, capsys: pytest.CaptureFixture[str]
) -> None:
    """Hard dup with --allow-duplicates → warning printed, exit code OK."""
    from agentalloy.reembed.cli import EXIT_OK, _report_dedup

    new_frag_id = "new-delta-f1"
    new_skill_id = "new-skill-delta"
    seeded_store.insert_embeddings(
        [_mk_fragment(new_frag_id, skill_id=new_skill_id, vec=_unit_vec(0))]
    )
    result = run_dedup_gate(
        new_skill_ids={new_skill_id},
        new_fragment_vecs={new_frag_id: (new_skill_id, _unit_vec(0))},
        vector_store=seeded_store,
        hard_similarity=_HARD,
        soft_similarity=_SOFT,
    )
    assert result.has_hard
    exit_code = _report_dedup(result, allow_duplicates=True)
    assert exit_code == EXIT_OK


def test_gate_hard_cross_pack_no_allow_duplicates(seeded_store: LanceFragmentStore) -> None:
    """Hard dup without --allow-duplicates → EXIT_DEDUP."""
    from agentalloy.reembed.cli import EXIT_DEDUP, _report_dedup

    new_frag_id = "new-epsilon-f1"
    new_skill_id = "new-skill-epsilon"
    seeded_store.insert_embeddings(
        [_mk_fragment(new_frag_id, skill_id=new_skill_id, vec=_unit_vec(0))]
    )
    result = run_dedup_gate(
        new_skill_ids={new_skill_id},
        new_fragment_vecs={new_frag_id: (new_skill_id, _unit_vec(0))},
        vector_store=seeded_store,
        hard_similarity=_HARD,
        soft_similarity=_SOFT,
    )
    assert result.has_hard
    exit_code = _report_dedup(result, allow_duplicates=False)
    assert exit_code == EXIT_DEDUP


# ---------------------------------------------------------------------------
# run_dedup_gate: soft duplicate
# ---------------------------------------------------------------------------


def test_gate_soft_cross_pack(seeded_store: LanceFragmentStore) -> None:
    """New skill at soft threshold → soft match, no hard."""
    new_frag_id = "new-soft-f1"
    new_skill_id = "new-skill-soft"
    vec = _mixed_vec(0, 99, 0.85)  # similarity ≈ 0.85 to existing-a-f1

    seeded_store.insert_embeddings([_mk_fragment(new_frag_id, skill_id=new_skill_id, vec=vec)])
    result = run_dedup_gate(
        new_skill_ids={new_skill_id},
        new_fragment_vecs={new_frag_id: (new_skill_id, vec)},
        vector_store=seeded_store,
        hard_similarity=_HARD,
        soft_similarity=_SOFT,
    )

    assert not result.has_hard
    assert result.has_soft
    soft_match = result.soft[0]
    assert soft_match.incoming_skill_id == new_skill_id
    assert soft_match.existing_skill_id == "existing-skill-a"
    assert soft_match.verdict == "soft"


def test_gate_soft_exit_ok(seeded_store: LanceFragmentStore) -> None:
    """Soft-only result → _report_dedup returns EXIT_OK."""
    from agentalloy.reembed.cli import EXIT_OK, _report_dedup

    result = DedupGateResult(
        soft=[
            DedupMatch(
                incoming_skill_id="new",
                existing_skill_id="old",
                fragment_id_incoming="nf1",
                fragment_id_existing="ef1",
                similarity=0.85,
                verdict="soft",
            )
        ]
    )
    assert _report_dedup(result, allow_duplicates=False) == EXIT_OK


# ---------------------------------------------------------------------------
# run_dedup_gate: same-pack exemption
# ---------------------------------------------------------------------------


def test_gate_same_pack_exempt(seeded_store: LanceFragmentStore) -> None:
    """Near-duplicate between two skills in the SAME new batch → no firing."""
    frag_a = "new-same-pack-a-f1"
    skill_a = "new-pack-skill-a"
    frag_b = "new-same-pack-b-f1"
    skill_b = "new-pack-skill-b"

    # Both share dim 0 — identical vectors but same pack.
    seeded_store.insert_embeddings(
        [
            _mk_fragment(frag_a, skill_id=skill_a, vec=_unit_vec(0)),
            _mk_fragment(frag_b, skill_id=skill_b, vec=_unit_vec(0)),
        ]
    )
    result = run_dedup_gate(
        new_skill_ids={skill_a, skill_b},
        new_fragment_vecs={
            frag_a: (skill_a, _unit_vec(0)),
            frag_b: (skill_b, _unit_vec(0)),
        },
        vector_store=seeded_store,
        hard_similarity=_HARD,
        soft_similarity=_SOFT,
    )

    # existing-skill-a is also at dim 0 and IS a cross-pack hit, but
    # skill_a vs skill_b matches are filtered.
    # The cross-pack hit against existing-skill-a will fire; this test
    # validates that intra-batch matches (skill_a ↔ skill_b) are excluded.
    for match in result.hard + result.soft:
        assert match.existing_skill_id not in {skill_a, skill_b}, (
            f"same-pack skill {match.existing_skill_id} should be exempt"
        )


# ---------------------------------------------------------------------------
# run_dedup_gate: existing corpus re-embed (no new skills → gate silent)
# ---------------------------------------------------------------------------


def test_gate_no_new_fragments(seeded_store: LanceFragmentStore) -> None:
    """Empty new_fragment_vecs → DedupGateResult with no findings."""
    result = run_dedup_gate(
        new_skill_ids=set(),
        new_fragment_vecs={},
        vector_store=seeded_store,
        hard_similarity=_HARD,
        soft_similarity=_SOFT,
    )
    assert not result.has_hard
    assert not result.has_soft


# ---------------------------------------------------------------------------
# Retry-path vector attribution (regression: order-correlated capture desyncs)
# ---------------------------------------------------------------------------


def test_on_embedded_attribution_survives_transient_retry(tmp_path: Path) -> None:
    """A transient embed failure must not shift vector→fragment attribution.

    The CLI records (fragment, vector) via reembed_fragments' on_embedded
    callback. The earlier wrapper correlated by embed_fn call order, which
    desyncs when _embed_with_retry calls embed_fn twice for one fragment.
    """
    from agentalloy.lm_client import LMTimeout
    from agentalloy.reembed.cli import FragmentNeedingEmbedding, reembed_fragments

    frags = [
        FragmentNeedingEmbedding(
            fragment_id=f"frag-{i}",
            skill_id=f"skill-{i}",
            category="engineering",
            fragment_type="execution",
            content=f"content {i}",
        )
        for i in range(3)
    ]
    vec_by_content = {f.content: _unit_vec(i) for i, f in enumerate(frags)}

    calls = {"n": 0}

    def flaky_embed(text: str) -> list[float]:
        calls["n"] += 1
        # First attempt on the second fragment fails transiently → retried.
        if text == "content 1" and calls["n"] == 2:
            raise LMTimeout("transient")
        return vec_by_content[text]

    recorded: dict[str, list[float]] = {}

    vs = LanceFragmentStore(tmp_path / "fragments.lance")
    try:
        stats = reembed_fragments(
            frags,
            embed_fn=flaky_embed,
            vector_store=vs,
            embedding_model="test-model",
            on_embedded=lambda f, v: recorded.update({f.fragment_id: v}),
        )
    finally:
        vs.close()

    assert stats.embedded == 3
    assert calls["n"] == 4  # one retry happened
    for i, f in enumerate(frags):
        assert recorded[f.fragment_id] == _unit_vec(i), f"attribution shifted for {f.fragment_id}"
