"""Store-level round trips for the OverGraph code index (P3 verification).

Covers the CodeGraphStore + CodeVectorStore surface the retrieval layer and
the knowledge index depend on: symbol/edge upsert and read-back, FQN
resolution fallbacks, file- and repo-scoped deletes (tombstone semantics),
the knowledge-edge methods (GOVERNS / entity edges), centrality, and the
vector leg (upsert / search / bulk_replace / delete) with deterministic fake
vectors — no embed server required.
"""

from __future__ import annotations

import time

import pytest

from agentalloy.code_index.open import open_codegraph
from agentalloy.code_index.protocols import (
    CodeEdge,
    CodeSymbol,
    CodeVectorRow,
    EmbeddingDimMismatchError,
)

VEC_DIM = 768


def _sym(
    qn: str,
    *,
    kind: str = "Function",
    name: str | None = None,
    file_path: str = "/repo/a.py",
    start: int = 1,
    end: int = 2,
    docstring: str | None = None,
    source_code: str | None = None,
    repo: str = "repo",
    content_hash: str | None = None,
) -> CodeSymbol:
    return CodeSymbol(
        qualified_name=qn,
        kind=kind,
        name=name or qn.rsplit(".", 1)[-1],
        file_path=file_path,
        start_line=start,
        end_line=end,
        docstring=docstring,
        decorators=[],
        is_exported=None,
        is_async=False,
        is_generator=False,
        source_code=source_code,
        content_hash=content_hash,
        repo=repo,
    )


def _vec(i: int) -> list[float]:
    """Deterministic non-zero 768-d vector with energy in slots i, i+1."""
    v = [0.0] * VEC_DIM
    v[i] = 1.0
    v[i + 1] = 0.5
    return v


def _row(qn: str, vec: list[float], text: str) -> CodeVectorRow:
    return CodeVectorRow(
        qualified_name=qn,
        embedding=vec,
        symbol_type="Function",
        file_path="/repo/a.py",
        start_line=1,
        end_line=2,
        text=text,
        indexed_at=int(time.time()),
    )


@pytest.fixture
def store(tmp_path):
    s = open_codegraph(tmp_path / "index")
    yield s
    s.close()


def test_symbol_round_trip(store):
    store.upsert_symbols(
        [
            _sym(
                "repo.mod.hello",
                docstring="Say hi.",
                source_code="def hello(): ...",
                file_path="/repo/mod.py",
                start=10,
                end=12,
                content_hash="h1",
            ),
            _sym("repo.mod.world", kind="Class", file_path="/repo/mod.py", content_hash="h2"),
        ]
    )
    got = store.symbol("repo.mod.hello")
    assert got is not None
    assert got.kind == "Function"
    assert got.name == "hello"
    assert got.file_path == "/repo/mod.py"
    assert got.start_line == 10
    assert got.end_line == 12
    assert got.docstring == "Say hi."
    assert got.source_code == "def hello(): ..."
    assert got.content_hash == "h1"
    assert got.repo == "repo"
    assert store.symbol("repo.mod.world").kind == "Class"
    assert store.symbol("repo.mod.nope") is None


def test_edges_callers_callees(store):
    store.upsert_symbols([_sym(f"mod.{n}", file_path=f"/repo/{n}.py") for n in ("a", "b", "c")])
    store.upsert_edges(
        [
            CodeEdge("mod.a", "mod.b", "CALLS", file_path="/repo/a.py"),
            CodeEdge("mod.b", "mod.c", "CALLS", file_path="/repo/b.py"),
        ]
    )
    callers = store.callers("mod.b")
    assert [c.qualified_name for c in callers] == ["mod.a"]
    callees = store.callees("mod.b")
    assert [c.qualified_name for c in callees] == ["mod.c"]
    assert set(store.calls_edges()) == {("mod.a", "mod.b"), ("mod.b", "mod.c")}
    assert store.callers("mod.nope") == []


def test_symbol_lookups(store):
    store.upsert_symbols(
        [
            _sym("pkg.app.hello", file_path="/repo/app.py"),
            _sym("pkg.lib.world", file_path="/repo/lib.py"),
        ]
    )
    assert store.symbols_matching("hello")
    assert any(s.qualified_name == "pkg.app.hello" for s in store.symbols_matching("hello"))
    assert not store.symbols_matching("zzz_not_there")
    assert any(qn == "pkg.app.hello" for qn, _ in store.symbols_by_name("hello"))
    assert any(qn == "pkg.app.hello" for qn, _ in store.symbols_by_file("/repo/app.py"))


def test_delete_for_files_tombstones(store):
    store.upsert_symbols(
        [
            _sym("pkg.alpha", file_path="/x/a.py"),
            _sym("pkg.beta", file_path="/x/a.py"),
            _sym("pkg.gamma", file_path="/x/b.py"),
        ]
    )
    store.upsert_edges(
        [
            CodeEdge("pkg.alpha", "pkg.gamma", "CALLS", file_path="/x/a.py"),
            CodeEdge("pkg.gamma", "pkg.beta", "CALLS", file_path="/x/b.py"),
        ]
    )
    store.delete_for_files(["/x/a.py"])

    # Symbols in the deleted file are tombstoned; the other file's symbol lives.
    assert store.symbol("pkg.alpha") is None
    assert store.symbol("pkg.beta") is None
    assert store.symbol("pkg.gamma") is not None
    # Edges are dropped by their OWN file_path: the a.py call is gone, the
    # b.py call survives (its tombstoned endpoint still resolves).
    assert store.calls_edges() == [("pkg.gamma", "pkg.beta")]
    assert store.callers("pkg.gamma") == []
    assert [c.qualified_name for c in store.callees("pkg.gamma")] == ["pkg.beta"]


def test_delete_for_repo_sweeps(store):
    store.upsert_symbols(
        [
            _sym("r1.app.a", file_path="/r1/a.py", repo="r1"),
            _sym("r1.app.b", file_path="/r1/b.py", repo="r1"),
            _sym("r2.app.c", file_path="/r2/c.py", repo="r2"),
            _sym(
                "r1/docs/solutions/d.md::x",
                kind="MarkdownDoc",
                file_path="/r1/docs/solutions/d.md",
                repo="r1",
            ),
        ]
    )
    store.upsert_edges(
        [
            CodeEdge("r1.app.a", "r1.app.b", "CALLS", file_path="/r1/a.py", repo="r1"),
            CodeEdge(
                "r1/docs/solutions/d.md::x",
                "r1.app.a",
                "GOVERNS",
                file_path="/r1/docs/solutions/d.md",
                span="app.a",
                resolution_tier=1,
                repo="r1",
            ),
        ]
    )
    removed = store.delete_for_repo("r1")

    # Repo sweep: every r1 symbol (code AND doc) is gone, r2 untouched.
    assert removed == 5  # 1 CALLS + 1 GOVERNS edges, 3 r1 symbols
    assert store.symbol("r1.app.a") is None
    assert store.symbol("r1.app.b") is None
    assert store.symbol("r1/docs/solutions/d.md::x") is None
    assert store.symbol("r2.app.c") is not None
    assert store.calls_edges() == []
    assert store.count_govern_edges_for_doc("/r1/docs/solutions/d.md") == 0


def test_content_hashes_and_meta(store):
    store.upsert_symbols(
        [
            _sym("repo.one", content_hash="aaa"),
            _sym("repo.two", content_hash=None),
        ]
    )
    assert store.content_hashes() == {"repo.one": "aaa"}
    store.set_meta("embed_model", "test-model")
    assert store.get_meta("embed_model") == "test-model"
    assert store.get_meta("missing") is None


def test_decision_doc_round_trip(store):
    doc_qn = "repo/docs/solutions/d.md::dec"
    doc_path = "/repo/docs/solutions/d.md"
    store.upsert_symbols(
        [
            _sym("repo.app.hello", file_path="/repo/app.py"),
            _sym(doc_qn, kind="MarkdownDoc", file_path=doc_path),
        ]
    )
    store.upsert_edges(
        [
            CodeEdge(
                doc_qn,
                "repo.app.hello",
                "GOVERNS",
                file_path=doc_path,
                span="app.hello",
                resolution_tier=1,
                repo="repo",
            )
        ]
    )
    assert store.decision_qns() == [doc_qn]

    rows = store.governing_decisions("repo.app.hello")
    assert [r.qualified_name for r in rows] == [doc_qn]
    assert rows[0].file_path == doc_path

    edges = store.governs_edges_for_symbol("repo.app.hello")
    assert len(edges) == 1
    assert edges[0].src == doc_qn
    assert edges[0].kind == "GOVERNS"
    assert edges[0].span == "app.hello"
    assert edges[0].resolution_tier == 1

    from_edges = store.governs_edges_from(doc_qn)
    assert [e.dst for e in from_edges] == ["repo.app.hello"]

    assert store.decision_docs_governing(["repo.app.hello"]) == [doc_path]

    # Tombstone-aware: a renamed-away symbol still reports its governing docs.
    store.delete_for_files(["/repo/app.py"])
    assert store.symbol("repo.app.hello") is None
    assert store.decision_docs_governing(["repo.app.hello"]) == [doc_path]

    # Scoped delete: only this doc's GOVERNS edges go.
    assert store.count_govern_edges_for_doc(doc_path) == 1
    assert store.delete_govern_edges_for_doc(doc_path) == 1
    assert store.count_govern_edges_for_doc(doc_path) == 0
    assert store.governs_edges_from(doc_qn) == []


def test_typed_edges_chunk_vs_fqn(store):
    chunk = "repo/docs/solutions/d.md::dec"
    doc_path = "/repo/docs/solutions/d.md"
    store.upsert_symbols(
        [
            _sym(chunk, kind="MarkdownDoc", file_path=doc_path),
            _sym("app.hello", file_path="/repo/app.py"),
            _sym("app.world", file_path="/repo/world.py"),
        ]
    )
    store.upsert_edges(
        [
            CodeEdge(
                chunk,
                "app.hello",
                "REQUIRES",
                file_path=doc_path,
                span="app.hello",
                resolution_tier=1,
                repo="repo",
            ),
            CodeEdge(
                chunk,
                "app.world",
                "TOUCHES",
                file_path=doc_path,
                span="app.world",
                resolution_tier=1,
                repo="repo",
            ),
        ]
    )
    # Outgoing view from a chunk keeps provenance (file_path / span / tier).
    out = store.typed_edges_from_chunks([chunk])
    assert {(e.dst, e.kind, e.resolution_tier) for e in out} == {
        ("app.hello", "REQUIRES", 1),
        ("app.world", "TOUCHES", 1),
    }
    assert all(e.file_path == doc_path and e.span for e in out)

    # Incoming view on the target fqn (the neighbors API drops props).
    inc = store.typed_edges_for_fqn("app.hello")
    assert [(e.src, e.dst, e.kind) for e in inc] == [(chunk, "app.hello", "REQUIRES")]
    assert store.typed_edges_for_fqn("app.nope") == []

    # Limit is honoured.
    assert len(store.typed_edges_from_chunks([chunk], limit=1)) == 1


def test_typed_edges_resolve_short_names(store):
    store.upsert_symbols(
        [
            _sym("app.util.parse_int", file_path="/repo/util.py"),
            _sym("app.util.parse_float", file_path="/repo/util.py"),
            _sym("docs/d.md::x", kind="MarkdownDoc", file_path="/docs/d.md"),
        ]
    )
    store.upsert_edges(
        [
            CodeEdge(
                "docs/d.md::x",
                "app.util.parse_int",
                "TOUCHES",
                file_path="/docs/d.md",
                span="parse_int",
                resolution_tier=2,
            )
        ]
    )
    # Exact qn works...
    exact = store.typed_edges_for_fqn("app.util.parse_int")
    assert [(e.src, e.kind) for e in exact] == [("docs/d.md::x", "TOUCHES")]
    # ...and the unique short-name fallback resolves to the same edge.
    short = store.typed_edges_for_fqn("parse_int")
    assert [(e.src, e.dst, e.kind) for e in short] == [
        ("docs/d.md::x", "app.util.parse_int", "TOUCHES")
    ]


def test_counts_list_files(store):
    store.upsert_symbols(
        [
            _sym("a.one", kind="Function", file_path="/repo/a/x.py"),
            _sym("a.two", kind="Function", file_path="/repo/a/y.py"),
            _sym("b.three", kind="Class", file_path="/repo/b/z.py"),
            _sym("d.md::h", kind="MarkdownDoc", file_path="/repo/d.md"),
        ]
    )
    assert store.counts_by_kind() == {"Function": 2, "Class": 1, "MarkdownDoc": 1}
    files = store.list_files()
    assert files == sorted(files)
    assert files == ["/repo/a/x.py", "/repo/a/y.py", "/repo/b/z.py", "/repo/d.md"]
    assert store.list_files(prefix="/repo/a") == ["/repo/a/x.py", "/repo/a/y.py"]
    assert store.list_files(limit=2, offset=2) == ["/repo/b/z.py", "/repo/d.md"]


def test_subgraph_shapes_and_filters(store):
    store.upsert_symbols(
        [_sym(f"mod.{n}", file_path=f"/r/{n}.py", repo="r") for n in ("a", "b", "c")]
    )
    store.upsert_edges(
        [
            CodeEdge("mod.a", "mod.b", "CALLS", file_path="/r/a.py", repo="r"),
            CodeEdge("mod.b", "mod.c", "CALLS", file_path="/r/b.py", repo="r"),
        ]
    )
    # Seed by unique short name (resolution fallback) — 2 hops reaches all.
    nodes, rels = store.subgraph(["a"], hops=2, limit=10)
    assert {n["qname"] for n in nodes} == {"mod.a", "mod.b", "mod.c"}
    hops = {n["qname"]: n["hop_distance"] for n in nodes}
    assert hops == {"mod.a": 0, "mod.b": 1, "mod.c": 2}
    for n in nodes:
        assert set(n) == {"id", "qname", "kind", "file", "repo", "centrality", "hop_distance"}
        assert n["repo"] == "r"
    pairs = {(r["source"], r["target"]) for r in rels}
    assert {("mod.a", "mod.b"), ("mod.b", "mod.c")} <= pairs
    for r in rels:
        assert set(r) == {"source", "target", "type", "confidence", "file"}

    # Repo filter drops foreign nodes and their relationships.
    nodes2, rels2 = store.subgraph(["a"], hops=2, limit=10, repo="other")
    assert nodes2 == [] and rels2 == []

    # Limit truncates nodes.
    nodes3, _ = store.subgraph(["a"], hops=3, limit=2)
    assert len(nodes3) == 2


def test_centrality_round_trip(store):
    store.upsert_symbols([_sym(f"mod.{n}", file_path=f"/r/{n}.py") for n in ("a", "b", "c")])
    assert store.write_centrality({"mod.a": 0.5, "mod.b": 0.3}) == 2
    assert store.read_centrality(["mod.a", "mod.b", "mod.c"]) == {
        "mod.a": 0.5,
        "mod.b": 0.3,
    }
    assert store.top_centrality(1) == [("mod.a", 0.5)]
    # Replace semantics: symbols absent from the new map lose their score.
    assert store.write_centrality({"mod.c": 0.9}) == 1
    assert store.read_centrality(["mod.a", "mod.b", "mod.c"]) == {"mod.c": 0.9}
    assert store.top_centrality() == [("mod.c", 0.9)]


def test_vector_upsert_search_delete(store):
    s1, s2, s3 = "m.one", "m.two", "m.three"
    store.upsert_symbols(
        [_sym(qn, source_code=f"def {qn.split('.')[-1]}(): ...") for qn in (s1, s2, s3)]
    )
    assert store.count() == 0
    assert store.embedding_dim() is None
    # No vectors yet: fts_docs composes fallback docs from graph props so a
    # lexical-only ingest (embed server down) still indexes something.
    assert {qn for qn, _ in store.fts_docs()} == {s1, s2, s3}
    assert all("def" in text for _, text in store.fts_docs())

    assert (
        store.upsert(
            [
                _row(s1, _vec(0), "pagerank over the call graph"),
                _row(s2, _vec(1), "hybrid search fusion"),
                _row(s3, _vec(2), "tantivy lexical side index"),
            ]
        )
        == 3
    )
    assert store.count() == 3
    assert store.embedding_dim() == VEC_DIM
    assert {qn for qn, _ in store.fts_docs()} == {s1, s2, s3}

    # Dense search: exact-match vector ranks first with cosine ~1.0.
    hits = store.search_similar(_vec(0), k=3)
    assert [h.qualified_name for h in hits] == [s1, s2, s3]
    assert hits[0].score > 0.99
    assert hits[0].file_path == "/repo/a.py"
    assert hits[0].start_line == 1 and hits[0].end_line == 2

    # GQL CONTAINS lexical fallback matches the stored embed text.
    assert [qn for qn, _ in store.search_bm25("pagerank")] == [s1]
    assert store.search_bm25("") == []

    # Dimension mismatches fail fast on both legs.
    with pytest.raises(EmbeddingDimMismatchError):
        store.search_similar([0.1, 0.2])
    with pytest.raises(EmbeddingDimMismatchError):
        store.upsert([_row(s1, [0.1] * 8, "short")])

    # Vector delete DETACH-deletes the live node.
    assert store.delete([s1]) == 1
    assert store.count() == 2
    assert store.symbol(s1) is None

    # bulk_replace clears vector membership of everyone not in the batch.
    s4 = "m.four"
    store.upsert_symbols([_sym(s4, source_code="def four(): ...")])
    assert store.bulk_replace([_row(s4, _vec(3), "fresh vector row")]) == 1
    assert store.count() == 1
    # s2/s3 lost vector membership but remain symbols — they stay in FTS via
    # the composed fallback; s4 contributes its stored embed text.
    assert {qn for qn, _ in store.fts_docs()} == {s2, s3, s4}
    assert dict(store.fts_docs())[s4] == "fresh vector row"
    # Stale HNSW entries are filtered out of similarity results.
    hits = store.search_similar(_vec(1), k=5)
    assert [h.qualified_name for h in hits] == [s4]
