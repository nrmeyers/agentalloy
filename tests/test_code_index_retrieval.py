"""Retrieval layer: tantivy FTS side index + hybrid CodeSearcher (P3).

The dense leg is exercised with stub embedders (fixed 768-d vectors) so the
full hybrid pipeline runs without the embed server; the lexical leg is
covered standalone.
"""

from __future__ import annotations

import pytest

from agentalloy.code_index.embed_client import EmbedError
from agentalloy.code_index.fts import FtsDoc, FtsIndex
from agentalloy.code_index.open import open_codegraph
from agentalloy.code_index.protocols import CodeSymbol, CodeVectorRow
from agentalloy.code_index.retrieval.hybrid import (
    MAX_EMBED_TEXT_CHARS,
    CodeSearcher,
    finalize_query_text,
    rewrite_query,
)

VEC_DIM = 768


def _sym(
    qn: str,
    *,
    docstring: str | None = None,
    file_path: str,
    start: int = 1,
    end: int = 2,
    repo: str = "smoke",
) -> CodeSymbol:
    return CodeSymbol(
        qualified_name=qn,
        kind="Function",
        name=qn.rsplit(".", 1)[-1],
        file_path=file_path,
        start_line=start,
        end_line=end,
        docstring=docstring,
        decorators=[],
        is_exported=None,
        is_async=False,
        is_generator=False,
        source_code=None,
        repo=repo,
    )


def _vec(i: int) -> list[float]:
    v = [0.0] * VEC_DIM
    v[i] = 1.0
    v[i + 1] = 0.5
    return v


class _StubEmbedder:
    """Answers every query with one fixed vector (dense leg, no server)."""

    def __init__(self, vec: list[float]) -> None:
        self._vec = vec

    def is_available(self) -> bool:
        return True

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [list(self._vec) for _ in texts]


class _DeadEmbedder:
    def is_available(self) -> bool:
        return True

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise EmbedError("embedder down")


@pytest.fixture
def store(tmp_path):
    s = open_codegraph(tmp_path / "index")
    yield s
    s.close()


# ---------------------------------------------------------------------------
# FTS side index
# ---------------------------------------------------------------------------


def test_fts_rebuild_count_search(tmp_path):
    fts = FtsIndex(tmp_path / "fts")
    assert not fts.exists()
    assert fts.search("pagerank") == []  # unbuilt → empty, never raises

    n = fts.rebuild(
        [
            FtsDoc("pkg.a", "pagerank over the call graph"),
            FtsDoc("pkg.b", "hybrid search fusion"),
        ]
    )
    assert n == 2
    assert fts.count() == 2
    hits = fts.search("pagerank", k=5)
    assert hits and hits[0][0] == "pkg.a" and hits[0][1] > 0.0
    assert fts.search("zzz_no_such_term") == []
    assert fts.search("") == []

    # Rebuild is wholesale: the old docs are gone.
    assert fts.rebuild([FtsDoc("pkg.c", "fresh index content")]) == 1
    assert fts.count() == 1
    assert fts.search("pagerank") == []
    fresh = fts.search("fresh")
    assert fresh and fresh[0][0] == "pkg.c"


# ---------------------------------------------------------------------------
# Query rewrite
# ---------------------------------------------------------------------------


def test_rewrite_query():
    # Short queries pass through untouched.
    assert rewrite_query("pagerank") == "pagerank"
    # Symbol-like tokens pass through even on long queries.
    assert (
        rewrite_query("find search_similar in the hybrid module")
        == "find search_similar in the hybrid module"
    )
    # Descriptive queries lose English filler.
    assert rewrite_query("how do I find pagerank in the code") == "pagerank code"


def test_finalize_query_text():
    assert finalize_query_text("pagerank").startswith("search_query: ")
    assert len(finalize_query_text("x" * 10_000)) == MAX_EMBED_TEXT_CHARS


# ---------------------------------------------------------------------------
# Hybrid searcher
# ---------------------------------------------------------------------------


def test_searcher_lexical_only(store, tmp_path):
    store.upsert_symbols(
        [
            _sym(
                "mod.hello", docstring="Greet the named party.", file_path="/s/a.py", start=5, end=7
            ),
            _sym("mod.world", docstring="World reference.", file_path="/s/b.py"),
        ]
    )
    fts = FtsIndex(tmp_path / "fts")
    fts.rebuild(
        [
            FtsDoc("mod.hello", "greet the named party hello function"),
            FtsDoc("mod.world", "world reference function"),
        ]
    )
    searcher = CodeSearcher(store, fts, None)
    assert not searcher.embed_available()

    hits = searcher.search("greet named party", k=5)
    assert searcher.last_mode == "lexical-only"
    assert len(hits) == 1
    h = hits[0]
    assert h.qualified_name == "mod.hello"
    assert h.kind == "Function"
    assert h.file_path == "/s/a.py"
    assert h.start_line == 5 and h.end_line == 7
    assert h.snippet == "Greet the named party."
    assert h.repo == "smoke"
    assert h.centrality is None
    assert h.score > 0.0

    assert searcher.search("", k=5) == []
    assert searcher.lexical("world")[0].qualified_name == "mod.world"


def test_searcher_hybrid_with_stub_embedder(store, tmp_path):
    s1, s2 = "m.one", "m.two"
    store.upsert_symbols([_sym(s1, file_path="/s/1.py"), _sym(s2, file_path="/s/2.py")])
    store.upsert(
        [
            CodeVectorRow(
                qualified_name=s1,
                embedding=_vec(0),
                symbol_type="Function",
                file_path="/s/1.py",
                start_line=1,
                end_line=2,
                text="one text",
                indexed_at=1,
            ),
            CodeVectorRow(
                qualified_name=s2,
                embedding=_vec(1),
                symbol_type="Function",
                file_path="/s/2.py",
                start_line=1,
                end_line=2,
                text="two text",
                indexed_at=1,
            ),
        ]
    )
    fts = FtsIndex(tmp_path / "fts")
    fts.rebuild(FtsDoc(qn, text) for qn, text in store.fts_docs())

    searcher = CodeSearcher(store, fts, _StubEmbedder(_vec(0)))
    assert searcher.embed_available()
    hits = searcher.search("find one", k=5)
    assert searcher.last_mode == "hybrid"
    assert hits, "hybrid search returned no hits"
    # The stub embeds every query as _vec(0): s1 is the exact dense match.
    assert hits[0].qualified_name == s1


def test_searcher_degrades_when_embed_fails(store, tmp_path):
    store.upsert_symbols([_sym("mod.alpha", file_path="/s/a.py")])
    fts = FtsIndex(tmp_path / "fts")
    fts.rebuild([FtsDoc("mod.alpha", "alpha beta gamma")])

    searcher = CodeSearcher(store, fts, _DeadEmbedder())
    hits = searcher.search("alpha", k=5)
    assert searcher.last_mode == "lexical-only"
    assert [h.qualified_name for h in hits] == ["mod.alpha"]


def test_hydrate_skips_missing_symbol(store, tmp_path):
    # An FTS doc with no graph row is dropped, not served as a ghost hit.
    fts = FtsIndex(tmp_path / "fts")
    fts.rebuild([FtsDoc("ghost.fn", "ghostly function text")])
    searcher = CodeSearcher(store, fts, None)
    assert searcher.lexical("ghostly") == []


def test_searcher_respects_k(store, tmp_path):
    store.upsert_symbols(
        [_sym(f"mod.{n}", file_path=f"/s/{n}.py") for n in ("one", "two", "three")]
    )
    fts = FtsIndex(tmp_path / "fts")
    fts.rebuild([FtsDoc(f"mod.{n}", f"common word {n}") for n in ("one", "two", "three")])
    searcher = CodeSearcher(store, fts, None)
    assert len(searcher.search("common word", k=1)) == 1
    assert len(searcher.search("common word", k=10)) == 3
