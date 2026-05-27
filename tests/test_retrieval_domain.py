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
        "engineering",
        "tooling",
        "governance",
        "meta",
    ]
    assert phase_to_categories("build") == [
        "build",
        "engineering",
        "tooling",
        "ops",
        "governance",
        "meta",
    ]
    assert phase_to_categories("ops") == [
        "ops",
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
        assert f.category in {"build", "ops", "governance", "meta"}


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
