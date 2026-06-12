"""AC-1..4 for the domain retrieval pipeline."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

import agentalloy.retrieval.domain as domain_module
from agentalloy.fixtures.loader import load_fixtures
from agentalloy.lm_client import LMModelNotLoaded
from agentalloy.reads import get_active_fragments
from agentalloy.reads.models import ActiveFragment
from agentalloy.retrieval.domain import (
    _rrf_fuse,  # pyright: ignore[reportPrivateUsage]
    diversity_select,
    phase_to_categories,
    retrieve_domain_candidates,
    skill_granular_select,
)
from agentalloy.retrieval.embedding_errors import (
    EmbeddingError,
    EmbeddingErrorCode,
    EmbeddingErrorResult,
)
from agentalloy.storage.ladybug import LadybugStore
from agentalloy.storage.vector_store import (
    BM25Hit,
    FragmentEmbedding,
    SimilarityHit,
    VectorStore,
    open_or_create,
)
from tests.support import StubLMClient


@pytest.fixture
def populated(tmp_path: Path) -> LadybugStore:
    s = LadybugStore(str(tmp_path / "ladybug"))
    s.open()
    s.migrate()
    load_fixtures(s)
    return s


@pytest.fixture
def populated_vectors(tmp_path: Path, populated: LadybugStore) -> VectorStore:
    """Pre-populated DuckDB vector store with embeddings for every active
    fragment in ``populated``. Embedding values come from ``StubLMClient`` —
    the same deterministic stub used by the retrieval path's embedder
    parameter, so query and corpus vectors are coherent for cosine ranking.
    """
    vs = open_or_create(tmp_path / "vectors.duck")
    stub = StubLMClient()
    fragments = get_active_fragments(populated)
    now = int(time.time())
    items = [
        FragmentEmbedding(
            fragment_id=f.fragment_id,
            embedding=stub.embed(model="stub-embed", texts=[f.content])[0],
            skill_id=f.skill_id,
            category=f.category,
            fragment_type=f.fragment_type,
            embedded_at=now,
            embedding_model="stub",
            prose=f.content,
        )
        for f in fragments
    ]
    vs.insert_embeddings(items)
    vs.rebuild_fts_index()
    return vs


# -------- phase_to_categories --------


def test_phase_to_categories_locked_mapping() -> None:
    # v5.4: includes corpus-vocabulary categories alongside the legacy ones
    assert phase_to_categories("spec") == ["spec", "design", "tooling", "governance", "meta"]
    assert phase_to_categories("design") == [
        "design",
        "engineering",
        "tooling",
        "governance",
        "meta",
    ]
    assert phase_to_categories("qa") == [
        "qa",
        "quality",
        "review",
        "design",
        "engineering",
        "tooling",
        "governance",
        "meta",
    ]
    assert phase_to_categories("build") == [
        "build",
        "design",
        "engineering",
        "tooling",
        "ops",
        "governance",
        "meta",
    ]
    assert phase_to_categories("ops") == [
        "ops",
        "design",
        "engineering",
        "tooling",
        "governance",
        "meta",
    ]
    assert phase_to_categories("meta") == ["meta", "tooling", "governance"]
    assert phase_to_categories("governance") == ["governance", "review", "quality", "meta"]


# -------- AC-1: eligibility filter --------


def test_only_domain_fragments_returned(
    populated: LadybugStore, populated_vectors: VectorStore
) -> None:
    result = retrieve_domain_candidates(
        populated,
        StubLMClient(),
        populated_vectors,
        task="fastapi routing",
        phase="design",
        domain_tags=None,
        k=10,
        embedding_model="stub-embed",
    )
    for f in result.candidates:
        assert f.skill_class == "domain"


def test_category_filter_narrows_to_phase(
    populated: LadybugStore, populated_vectors: VectorStore
) -> None:
    # 'build' phase retrieves build/ops/governance/meta — but only domain fragments,
    # so design-only fragments must be excluded.
    result = retrieve_domain_candidates(
        populated,
        StubLMClient(),
        populated_vectors,
        task="write a migration",
        phase="build",
        domain_tags=None,
        k=10,
        embedding_model="stub-embed",
    )
    for f in result.candidates:
        assert f.category in {"build", "design", "ops", "governance", "meta"}


def test_domain_tags_narrow_further(
    populated: LadybugStore, populated_vectors: VectorStore
) -> None:
    result = retrieve_domain_candidates(
        populated,
        StubLMClient(),
        populated_vectors,
        task="fastapi",
        phase="design",
        domain_tags=["fastapi"],
        k=10,
        embedding_model="stub-embed",
    )
    assert result.candidates
    for f in result.candidates:
        assert "fastapi" in f.domain_tags


# -------- AC-2: ranking --------
#
# Ranking by cosine similarity now happens in DuckDB via
# ``array_cosine_distance`` — see ``test_vector_store.py`` for the
# corresponding tests. The previous in-Python ranking test against
# ``ActiveFragment.embedding`` is obsolete with the v5.3 storage split.


# -------- AC-3: structural diversity --------


def _fake(frag_id: str, ftype: str) -> ActiveFragment:
    return ActiveFragment(
        fragment_id=frag_id,
        fragment_type=ftype,
        sequence=1,
        content="",
        skill_id="s",
        version_id="s-v1",
        skill_class="domain",
        category="design",
        domain_tags=[],
    )


def test_diversity_prefers_setup_execution_verification_when_available() -> None:
    # Pool ordered by score: e1, e2, s1, v1, ex1
    pool = [
        _fake("e1", "execution"),
        _fake("e2", "execution"),
        _fake("s1", "setup"),
        _fake("v1", "verification"),
        _fake("ex1", "example"),
    ]
    selected = diversity_select(pool, k=3)
    types = [f.fragment_type for f in selected]
    # Should prefer to cover setup + execution + verification, not three executions.
    assert set(types) == {"setup", "execution", "verification"}


def test_diversity_returns_all_executions_when_only_executions_available() -> None:
    pool = [_fake("e1", "execution"), _fake("e2", "execution"), _fake("e3", "execution")]
    selected = diversity_select(pool, k=3)
    assert [f.fragment_type for f in selected] == ["execution", "execution", "execution"]


def test_diversity_respects_k_bound() -> None:
    pool = [_fake(f"x{i}", "execution") for i in range(10)]
    selected = diversity_select(pool, k=4)
    assert len(selected) == 4


def test_diversity_does_not_duplicate() -> None:
    pool = [_fake("a", "execution"), _fake("b", "setup")]
    selected = diversity_select(pool, k=5)  # k > pool size
    assert len(selected) == 2
    assert len({f.fragment_id for f in selected}) == 2


# -------- AC-5: skill-granular selection --------


def _fake_skill(frag_id: str, ftype: str, skill_id: str) -> ActiveFragment:
    return ActiveFragment(
        fragment_id=frag_id,
        fragment_type=ftype,
        sequence=1,
        content="",
        skill_id=skill_id,
        version_id=f"{skill_id}-v1",
        skill_class="domain",
        category="design",
        domain_tags=[],
    )


def test_skill_granular_sibling_cannibalization_regression() -> None:
    # Regression: old fragment-level diversity_select with k=4 would select only A and B
    # (ranks 1–4 are all A/B), crowding out C. skill_granular_select must include all three.
    #
    # Pool (rank order):
    #   rank 1 — skill typescript-narrowing-basic    frag A1 execution
    #   rank 2 — skill typescript-narrowing-basic    frag A2 setup
    #   rank 3 — skill typescript-narrowing-advanced frag B1 execution
    #   rank 4 — skill typescript-narrowing-advanced frag B2 setup
    #   rank 5 — skill testing-tdd-cycle             frag C1 verification
    pool = [
        _fake_skill("A1", "execution", "typescript-narrowing-basic"),
        _fake_skill("A2", "setup", "typescript-narrowing-basic"),
        _fake_skill("B1", "execution", "typescript-narrowing-advanced"),
        _fake_skill("B2", "setup", "typescript-narrowing-advanced"),
        _fake_skill("C1", "verification", "testing-tdd-cycle"),
    ]
    selected, skills_ranked = skill_granular_select(pool, k=4)

    selected_skill_ids = {f.skill_id for f in selected}
    # All three skills must be represented in the selected set.
    assert selected_skill_ids == {
        "typescript-narrowing-basic",
        "typescript-narrowing-advanced",
        "testing-tdd-cycle",
    }
    assert skills_ranked[:3] == [
        "typescript-narrowing-basic",
        "typescript-narrowing-advanced",
        "testing-tdd-cycle",
    ]
    assert len(selected) == 4


def test_skill_granular_round_robin_allocation() -> None:
    # 2 skills × 3 fragments each, k=4 → each skill contributes exactly 2 fragments.
    pool = [
        _fake_skill("S1-e", "execution", "skill-one"),
        _fake_skill("S2-e", "execution", "skill-two"),
        _fake_skill("S1-s", "setup", "skill-one"),
        _fake_skill("S2-s", "setup", "skill-two"),
        _fake_skill("S1-v", "verification", "skill-one"),
        _fake_skill("S2-v", "verification", "skill-two"),
    ]
    selected, skills_ranked = skill_granular_select(pool, k=4)

    assert len(selected) == 4
    from collections import Counter

    counts = Counter(f.skill_id for f in selected)
    assert counts["skill-one"] == 2
    assert counts["skill-two"] == 2
    assert set(skills_ranked) == {"skill-one", "skill-two"}


def test_skill_granular_top_skill_depth_guarantee() -> None:
    # 4 skills × 3 fragments each, k=4 → the top-ranked skill gets k//2 = 2
    # slots (depth guarantee); the next two skills get 1 each (breadth);
    # the 4th skill is squeezed out. Strict 1-per-skill round-robin starved
    # the gold skill of its convention-bearing fragments.
    pool = []
    for sid in ["gold", "sib-a", "sib-b", "sib-c"]:
        for i, ftype in enumerate(["execution", "setup", "verification"]):
            pool.append(_fake_skill(f"{sid}-f{i}", ftype, sid))
    # interleave so first fragment of each skill appears in rank order
    interleaved = [pool[j * 3 + i] for i in range(3) for j in range(4)]
    selected, skills_ranked = skill_granular_select(interleaved, k=4)

    from collections import Counter

    counts = Counter(f.skill_id for f in selected)
    assert counts["gold"] == 2
    assert counts["sib-a"] == 1
    assert counts["sib-b"] == 1
    assert "sib-c" not in counts
    assert skills_ranked[0] == "gold"


def test_skill_granular_depth_noop_for_k1() -> None:
    # k=1 → k//2 == 0 depth slots; pure breadth, top skill still wins slot 1.
    pool = [
        _fake_skill("g-0", "execution", "gold"),
        _fake_skill("s-0", "execution", "sib"),
    ]
    selected, _ = skill_granular_select(pool, k=1)
    assert [f.skill_id for f in selected] == ["gold"]


def test_skill_granular_top_skill_drains_when_others_exhausted() -> None:
    # 2 skills, k=5: gold depth 2, sib contributes its 1 fragment, remaining
    # budget returns to gold (stage 3 drain).
    pool = [
        _fake_skill("g-0", "execution", "gold"),
        _fake_skill("s-0", "setup", "sib"),
        _fake_skill("g-1", "setup", "gold"),
        _fake_skill("g-2", "verification", "gold"),
        _fake_skill("g-3", "rationale", "gold"),
    ]
    selected, _ = skill_granular_select(pool, k=5)
    from collections import Counter

    counts = Counter(f.skill_id for f in selected)
    assert counts["gold"] == 4
    assert counts["sib"] == 1


def test_skill_granular_fewer_skills_than_k() -> None:
    # 1 skill × 5 fragments, k=3 → all 3 come from that skill (matches old behavior).
    pool = [_fake_skill(f"s1-f{i}", "execution", "only-skill") for i in range(5)]
    selected, skills_ranked = skill_granular_select(pool, k=3)

    assert len(selected) == 3
    assert all(f.skill_id == "only-skill" for f in selected)
    assert skills_ranked == ["only-skill"]


def test_skill_granular_diversity_preference_across_skills() -> None:
    # When a setup/execution/verification type is not yet globally selected,
    # prefer it even if it is not the skill's top-ranked fragment.
    # Pool: skill-a has execution first, skill-b has execution first.
    # Round 1 should pick execution from skill-a, then prefer setup from skill-b
    # (setup not yet globally covered) rather than execution from skill-b.
    pool = [
        _fake_skill("A1", "execution", "skill-a"),
        _fake_skill("A2", "setup", "skill-a"),
        _fake_skill("B1", "execution", "skill-b"),
        _fake_skill("B2", "setup", "skill-b"),
        _fake_skill("B3", "verification", "skill-b"),
    ]
    selected, _ = skill_granular_select(pool, k=4)

    types = [f.fragment_type for f in selected]
    # setup and verification must appear — diversity applies globally.
    assert "setup" in types
    assert "verification" in types


def test_skill_granular_empty_input() -> None:
    selected, skills_ranked = skill_granular_select([], k=5)
    assert selected == []
    assert skills_ranked == []


def test_skill_granular_skills_ranked_populated_on_retrieval(
    populated: LadybugStore, populated_vectors: VectorStore
) -> None:
    # RetrievalResult.skills_ranked must be populated on a real retrieval.
    result = retrieve_domain_candidates(
        populated,
        StubLMClient(),
        populated_vectors,
        task="fastapi endpoint design",
        phase="design",
        domain_tags=None,
        k=5,
        embedding_model="stub-embed",
    )
    assert isinstance(result, domain_module.RetrievalResult)
    # skills_ranked must contain at least the skill ids present in candidates.
    candidate_skill_ids = {f.skill_id for f in result.candidates}
    ranked_set = set(result.skills_ranked)
    assert candidate_skill_ids <= ranked_set


# -------- AC-4: empty handling --------


def test_empty_eligible_returns_empty_result(
    populated: LadybugStore, populated_vectors: VectorStore
) -> None:
    # No fragments match a nonsense domain_tag
    result = retrieve_domain_candidates(
        populated,
        StubLMClient(),
        populated_vectors,
        task="irrelevant",
        phase="design",
        domain_tags=["nonexistent-tag"],
        k=10,
        embedding_model="stub-embed",
    )
    assert result.candidates == []
    assert result.eligible_count == 0
    assert result.retrieval_ms >= 0


def test_retrieval_records_latency(populated: LadybugStore, populated_vectors: VectorStore) -> None:
    result = retrieve_domain_candidates(
        populated,
        StubLMClient(),
        populated_vectors,
        task="t",
        phase="design",
        domain_tags=None,
        k=5,
        embedding_model="stub-embed",
    )
    assert result.retrieval_ms >= 0


def test_circuit_open_falls_back_to_bm25(
    populated: LadybugStore,
    populated_vectors: VectorStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(domain_module.embedding_breaker, "allow_request", lambda: False)

    result = retrieve_domain_candidates(
        populated,
        StubLMClient(),
        populated_vectors,
        task="fastapi endpoint design",
        phase="design",
        domain_tags=None,
        k=5,
        embedding_model="stub-embed",
    )

    assert isinstance(result, EmbeddingErrorResult)
    assert result.error.code == EmbeddingErrorCode.CIRCUIT_OPEN
    assert result.bm25_only is True
    assert result.candidates
    assert result.retrieval_ms >= 0


def test_embedding_error_also_falls_back_to_bm25(
    populated: LadybugStore,
    populated_vectors: VectorStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise_embed(*args: object, **kwargs: object) -> list[list[float]]:
        raise EmbeddingError(EmbeddingErrorCode.UNAVAILABLE, "embed down")

    monkeypatch.setattr(domain_module, "safe_embed", _raise_embed)

    result = retrieve_domain_candidates(
        populated,
        StubLMClient(),
        populated_vectors,
        task="fastapi endpoint design",
        phase="design",
        domain_tags=None,
        k=5,
        embedding_model="stub-embed",
    )

    assert isinstance(result, EmbeddingErrorResult)
    assert result.error.code == EmbeddingErrorCode.UNAVAILABLE
    assert result.bm25_only is True
    assert result.candidates


def test_model_not_loaded_does_not_degrade(
    populated: LadybugStore,
    populated_vectors: VectorStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise_embed(*args: object, **kwargs: object) -> list[list[float]]:
        original = LMModelNotLoaded("stub-embed", ["other-model"])
        raise EmbeddingError(
            EmbeddingErrorCode.MODEL_NOT_LOADED,
            str(original),
            original=original,
        )

    monkeypatch.setattr(domain_module, "safe_embed", _raise_embed)

    with pytest.raises(LMModelNotLoaded):
        retrieve_domain_candidates(
            populated,
            StubLMClient(),
            populated_vectors,
            task="fastapi endpoint design",
            phase="design",
            domain_tags=None,
            k=5,
            embedding_model="stub-embed",
        )


def test_k_larger_than_eligible_returns_all(
    populated: LadybugStore, populated_vectors: VectorStore
) -> None:
    result = retrieve_domain_candidates(
        populated,
        StubLMClient(),
        populated_vectors,
        task="t",
        phase="design",
        domain_tags=["fastapi"],
        k=50,
        embedding_model="stub-embed",
    )
    # Only a handful of fastapi-tagged fragments exist; k=50 must not error
    assert len(result.candidates) <= 50


# -------- _rrf_fuse --------


def _dense(fid: str) -> SimilarityHit:
    return SimilarityHit(fragment_id=fid, skill_id="s", distance=0.5)


def test_rrf_fuse_doc_in_both_legs_ranks_higher() -> None:
    # "shared" appears in both legs; "dense-only" / "bm25-only" each in one.
    dense = [_dense("shared"), _dense("dense-only")]
    bm25 = ["shared", "bm25-only"]
    result = _rrf_fuse(dense, bm25)
    # "shared" should rank first (contributions from both legs).
    assert result[0] == "shared"


def test_rrf_fuse_returns_union_of_both_legs() -> None:
    dense = [_dense("a"), _dense("b")]
    bm25 = ["b", "c"]
    result = _rrf_fuse(dense, bm25)
    assert set(result) == {"a", "b", "c"}


def test_rrf_fuse_empty_bm25_returns_dense_order() -> None:
    dense = [_dense("x"), _dense("y"), _dense("z")]
    result = _rrf_fuse(dense, [])
    # Without BM25 leg, RRF still ranks by dense order.
    assert result[0] == "x"


def test_rrf_fuse_empty_dense_returns_bm25_order() -> None:
    result = _rrf_fuse([], ["p", "q", "r"])
    assert result[0] == "p"


def test_rrf_fuse_both_empty_returns_empty() -> None:
    assert _rrf_fuse([], []) == []


def test_degradable_embedding_error_with_empty_bm25(
    populated: LadybugStore,
    populated_vectors: VectorStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test: embedding fails with degradable code AND BM25 returns no hits.

    The double-failure path must still return a structured EmbeddingErrorResult
    (candidates=[], bm25_only=True) rather than crashing.
    """

    def _raise_embed(*args: object, **kwargs: object) -> list[list[float]]:
        raise EmbeddingError(EmbeddingErrorCode.UNAVAILABLE, "embed down")

    monkeypatch.setattr(domain_module, "safe_embed", _raise_embed)

    def _empty_bm25(*args: object, **kwargs: object) -> list[BM25Hit]:
        return []

    monkeypatch.setattr(populated_vectors, "search_bm25", _empty_bm25)

    result = retrieve_domain_candidates(
        populated,
        StubLMClient(),
        populated_vectors,
        task="fastapi endpoint design",
        phase="design",
        domain_tags=None,
        k=5,
        embedding_model="stub-embed",
    )

    assert isinstance(result, EmbeddingErrorResult)
    assert result.error.code == EmbeddingErrorCode.UNAVAILABLE
    assert result.bm25_only is True
    assert result.candidates == []
    assert result.retrieval_ms >= 0


# -------- phase_to_scope_terms --------


def test_phase_to_scope_terms_mapping() -> None:
    from agentalloy.retrieval.domain import phase_to_scope_terms

    assert phase_to_scope_terms("build") == ["build"]
    assert phase_to_scope_terms("qa") == ["qa", "review"]  # authored vocab uses 'review'
    assert phase_to_scope_terms("governance") == ["governance", "review"]
    assert phase_to_scope_terms("meta") == []  # category map only


# -------- Stage A: cross-encoder rerank --------


class _FakeReranker:
    """Reranker protocol stub; records calls so bypass paths can be asserted."""

    def __init__(self, scores: list[float]) -> None:
        self._scores = scores
        self.calls: list[tuple[str, list[str]]] = []

    def score(self, query: str, passages: list[str]) -> list[float]:
        self.calls.append((query, list(passages)))
        return list(self._scores[: len(passages)])


class _RaisingReranker:
    def __init__(self) -> None:
        self.call_count = 0

    def score(self, query: str, passages: list[str]) -> list[float]:  # noqa: ARG002
        self.call_count += 1
        raise RuntimeError("boom")


def _content_skill(frag_id: str, content: str, skill_id: str) -> ActiveFragment:
    f = _fake_skill(frag_id, "execution", skill_id)
    return ActiveFragment(
        fragment_id=f.fragment_id,
        fragment_type=f.fragment_type,
        sequence=f.sequence,
        content=content,
        skill_id=f.skill_id,
        version_id=f.version_id,
        skill_class=f.skill_class,
        category=f.category,
        domain_tags=f.domain_tags,
    )


def _patch_reranker(monkeypatch: pytest.MonkeyPatch, reranker: object | None) -> None:
    monkeypatch.setattr(domain_module, "build_reranker_from_env", lambda: reranker)


def test_maybe_rerank_reorders_skills_by_score(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentalloy.retrieval.domain import _maybe_rerank  # pyright: ignore[reportPrivateUsage]

    # Skills A, B, C in rank order; fake scores make C best, A middle, B worst.
    ranked = [
        _content_skill("A1", "alpha one", "skill-A"),
        _content_skill("A2", "alpha two", "skill-A"),
        _content_skill("B1", "beta one", "skill-B"),
        _content_skill("C1", "gamma one", "skill-C"),
    ]
    fake = _FakeReranker([0.5, 0.1, 0.9])  # A=0.5, B=0.1, C=0.9
    _patch_reranker(monkeypatch, fake)

    rebuilt, reranked = _maybe_rerank(ranked, "my task")

    assert reranked is True
    assert [f.fragment_id for f in rebuilt] == ["C1", "A1", "A2", "B1"]
    # Passage = skill identity prefix + best fragment; within-skill order preserved.
    assert fake.calls == [
        ("my task", ["skill A: alpha one", "skill B: beta one", "skill C: gamma one"])
    ]


def test_maybe_rerank_respects_max_pairs(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentalloy.retrieval.domain import _maybe_rerank  # pyright: ignore[reportPrivateUsage]

    monkeypatch.setenv("RUNTIME_RERANK_MAX_PAIRS", "2")
    ranked = [
        _content_skill("A1", "a", "skill-A"),
        _content_skill("B1", "b", "skill-B"),
        _content_skill("C1", "c", "skill-C"),
    ]
    # Only first 2 skills are scored; B beats A. C is beyond the cap → keeps trailing.
    fake = _FakeReranker([0.1, 0.9])
    _patch_reranker(monkeypatch, fake)

    rebuilt, reranked = _maybe_rerank(ranked, "task")

    assert reranked is True
    assert [f.fragment_id for f in rebuilt] == ["B1", "A1", "C1"]
    assert fake.calls[0][1] == ["skill A: a", "skill B: b"]  # only the top 2 skills scored


def test_maybe_rerank_disabled_returns_original(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentalloy.retrieval.domain import _maybe_rerank  # pyright: ignore[reportPrivateUsage]

    _patch_reranker(monkeypatch, None)
    ranked = [_content_skill("A1", "a", "skill-A"), _content_skill("B1", "b", "skill-B")]
    rebuilt, reranked = _maybe_rerank(ranked, "task")
    assert reranked is False
    assert rebuilt is ranked


def test_maybe_rerank_scorer_failure_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentalloy.retrieval.domain import _maybe_rerank  # pyright: ignore[reportPrivateUsage]

    raising = _RaisingReranker()
    _patch_reranker(monkeypatch, raising)
    ranked = [_content_skill("A1", "a", "skill-A"), _content_skill("B1", "b", "skill-B")]
    rebuilt, reranked = _maybe_rerank(ranked, "task")
    assert reranked is False
    assert [f.fragment_id for f in rebuilt] == ["A1", "B1"]


def test_retrieve_sets_reranked_flag(
    populated: LadybugStore,
    populated_vectors: VectorStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A scorer that reverses ranking — any non-trivial pool gets reordered.
    captured: list[tuple[str, list[str]]] = []

    class _Reverse:
        def score(self, query: str, passages: list[str]) -> list[float]:
            captured.append((query, list(passages)))
            return [float(i) for i in range(len(passages))]

    _patch_reranker(monkeypatch, _Reverse())
    result = retrieve_domain_candidates(
        populated,
        StubLMClient(),
        populated_vectors,
        task="fastapi endpoint design",
        phase="design",
        domain_tags=None,
        k=5,
        embedding_model="stub-embed",
    )
    assert isinstance(result, domain_module.RetrievalResult)
    assert result.reranked is True
    assert captured  # scorer was invoked on the default path


def test_raw_scores_bypass_skips_reranker(
    populated: LadybugStore,
    populated_vectors: VectorStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeReranker([1.0] * 64)
    _patch_reranker(monkeypatch, fake)
    result = retrieve_domain_candidates(
        populated,
        StubLMClient(),
        populated_vectors,
        task="fastapi endpoint design",
        phase="design",
        domain_tags=None,
        k=5,
        embedding_model="stub-embed",
        raw_scores=True,
    )
    assert isinstance(result, domain_module.RetrievalResult)
    assert result.reranked is False
    assert fake.calls == []  # bypass path must not invoke the scorer


def test_diversity_off_bypass_skips_reranker(
    populated: LadybugStore,
    populated_vectors: VectorStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeReranker([1.0] * 64)
    _patch_reranker(monkeypatch, fake)
    monkeypatch.setenv("RUNTIME_DIVERSITY_SELECTION", "off")
    result = retrieve_domain_candidates(
        populated,
        StubLMClient(),
        populated_vectors,
        task="fastapi endpoint design",
        phase="design",
        domain_tags=None,
        k=5,
        embedding_model="stub-embed",
    )
    assert isinstance(result, domain_module.RetrievalResult)
    assert result.reranked is False
    assert fake.calls == []
