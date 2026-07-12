"""DuckDBCodeGraphStore — DDL, writes, relations, centrality, meta."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from agentalloy.code_index.store.graph_store import DuckDBCodeGraphStore
from agentalloy.storage.protocols import CodeEdge, CodeGraphStore, CodeSymbol


def sym(
    qn: str, *, kind: str = "Function", file_path: str | None = None, **kw: object
) -> CodeSymbol:
    defaults: dict[str, object] = {
        "name": qn.rsplit(".", 1)[-1],
        "start_line": 1,
        "end_line": 5,
        "docstring": None,
        "decorators": [],
        "is_exported": None,
        "is_async": False,
        "is_generator": False,
        "source_code": None,
    }
    defaults.update(kw)
    return CodeSymbol(qualified_name=qn, kind=kind, file_path=file_path, **defaults)  # type: ignore[arg-type]


def call(src: str, dst: str, *, file_path: str = "", line_start: int = 0) -> CodeEdge:
    return CodeEdge(src=src, dst=dst, kind="CALLS", file_path=file_path, line_start=line_start)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBCodeGraphStore]:
    s = DuckDBCodeGraphStore(tmp_path / "graph.duck")
    s.migrate()
    yield s
    s.close()


# Fixture graph:  a --calls--> c --calls--> d --calls--> a  (cycle)
#                 b --calls--> c
FIXTURE_SYMBOLS = [
    sym(
        "mod.a",
        file_path="mod/a.py",
        start_line=10,
        end_line=20,
        docstring="does a",
        decorators=["cached", "retry"],
        is_exported=True,
        is_async=True,
        source_code="async def a(): ...",
        contextual_prefix="module mod",
        content_hash="hash-a",
    ),
    sym("mod.b", file_path="mod/b.py", content_hash="hash-b"),
    sym("mod.c", kind="Method", file_path="mod/a.py", start_line=30, content_hash="hash-c"),
    sym("mod.d", file_path="mod/d.py", start_line=7),
    sym("mod.M", kind="Class", file_path="mod/a.py"),
]
FIXTURE_EDGES = [
    call("mod.a", "mod.c", file_path="mod/a.py", line_start=12),
    call("mod.b", "mod.c", file_path="mod/b.py", line_start=3),
    call("mod.c", "mod.d", file_path="mod/a.py", line_start=33),
    call("mod.d", "mod.a", file_path="mod/d.py", line_start=8),
    CodeEdge(src="mod.M", dst="mod.c", kind="CONTAINS"),
]


@pytest.fixture
def populated(store: DuckDBCodeGraphStore) -> DuckDBCodeGraphStore:
    store.replace_all(FIXTURE_SYMBOLS, FIXTURE_EDGES)
    return store


def test_satisfies_protocol(store: DuckDBCodeGraphStore) -> None:
    assert isinstance(store, CodeGraphStore)


def test_migrate_idempotent(tmp_path: Path) -> None:
    s = DuckDBCodeGraphStore(tmp_path / "graph.duck")
    s.migrate()
    s.migrate()  # second run must be a no-op, not an error
    assert s.counts_by_kind() == {}
    s.close()


def test_replace_all_counts_and_lookup(populated: DuckDBCodeGraphStore) -> None:
    got = populated.symbol("mod.a")
    assert got is not None
    assert got.kind == "Function"
    assert got.file_path == "mod/a.py"
    assert got.start_line == 10 and got.end_line == 20
    assert got.docstring == "does a"
    assert got.decorators == ["cached", "retry"]
    assert got.is_exported is True
    assert got.is_async is True and got.is_generator is False
    assert got.source_code == "async def a(): ..."
    assert got.contextual_prefix == "module mod"
    assert got.content_hash == "hash-a"
    assert populated.symbol("mod.nope") is None


def test_replace_all_replaces_prior_contents(populated: DuckDBCodeGraphStore) -> None:
    n_sym, n_edge = populated.replace_all([sym("other.x", file_path="x.py")], [])
    assert (n_sym, n_edge) == (1, 0)
    assert populated.symbol("mod.a") is None
    assert populated.symbol("other.x") is not None
    assert populated.calls_edges() == []


def test_upsert_symbols_replaces_on_key(populated: DuckDBCodeGraphStore) -> None:
    updated = sym("mod.b", file_path="mod/b.py", docstring="now documented")
    assert populated.upsert_symbols([updated]) == 1
    got = populated.symbol("mod.b")
    assert got is not None and got.docstring == "now documented"
    assert populated.counts_by_kind()["Function"] == 3  # no duplicate row


def test_upsert_edges_appends(populated: DuckDBCodeGraphStore) -> None:
    assert populated.upsert_edges([call("mod.b", "mod.d")]) == 1
    assert ("mod.b", "mod.d") in populated.calls_edges()
    assert populated.upsert_edges([]) == 0


def test_delete_for_files(populated: DuckDBCodeGraphStore) -> None:
    # mod/a.py holds symbols a, c, M and edges a->c, c->d.
    removed = populated.delete_for_files(["mod/a.py"])
    assert removed == 5
    assert populated.symbol("mod.a") is None
    assert populated.symbol("mod.c") is None
    assert populated.symbol("mod.b") is not None  # other files untouched
    remaining = set(populated.calls_edges())
    assert remaining == {("mod.b", "mod.c"), ("mod.d", "mod.a")}
    assert populated.delete_for_files([]) == 0


def test_callers_single_join(populated: DuckDBCodeGraphStore) -> None:
    hits = populated.callers("mod.c")
    assert [h.qualified_name for h in hits] == ["mod.a", "mod.b"]
    by_qn = {h.qualified_name: h for h in hits}
    # file_path is the caller's (denormalized on symbols); line is the call site.
    assert by_qn["mod.a"].file_path == "mod/a.py"
    assert by_qn["mod.a"].line == 12
    assert by_qn["mod.b"].file_path == "mod/b.py"
    assert by_qn["mod.b"].line == 3
    # CONTAINS edge from mod.M must not appear as a caller.
    assert "mod.M" not in by_qn


def test_callers_dangling_endpoint_uses_edge_file(populated: DuckDBCodeGraphStore) -> None:
    populated.upsert_edges([call("ext.pkg.fn", "mod.c", file_path="vendor/x.py", line_start=4)])
    hits = {h.qualified_name: h for h in populated.callers("mod.c")}
    assert hits["ext.pkg.fn"].file_path == "vendor/x.py"


def test_callees_single_join(populated: DuckDBCodeGraphStore) -> None:
    hits = populated.callees("mod.c")
    assert [h.qualified_name for h in hits] == ["mod.d"]
    assert hits[0].file_path == "mod/d.py"
    assert hits[0].line == 7  # callee's definition line
    assert populated.callees("mod.b") == [
        type(hits[0])(qualified_name="mod.c", file_path="mod/a.py", line=30)
    ]


def test_transitive_callers_depth_cap(populated: DuckDBCodeGraphStore) -> None:
    # callers of d: depth1 = {c}; depth2 adds {a, b}.
    depth1 = {h.qualified_name for h in populated.transitive_callers("mod.d", max_depth=1)}
    assert depth1 == {"mod.c"}
    depth2 = {h.qualified_name for h in populated.transitive_callers("mod.d", max_depth=2)}
    assert depth2 == {"mod.c", "mod.a", "mod.b"}
    assert populated.transitive_callers("mod.d", max_depth=0) == []


def test_transitive_callers_cycle_terminates_and_excludes_seed(
    populated: DuckDBCodeGraphStore,
) -> None:
    # a -> c -> d -> a is a cycle; deep traversal must terminate and never
    # report the seed itself.
    hits = populated.transitive_callers("mod.a", max_depth=50)
    qns = {h.qualified_name for h in hits}
    assert qns == {"mod.d", "mod.c", "mod.b"}
    assert "mod.a" not in qns


def test_counts_by_kind(populated: DuckDBCodeGraphStore) -> None:
    assert populated.counts_by_kind() == {"Function": 3, "Method": 1, "Class": 1}


def test_list_files(populated: DuckDBCodeGraphStore) -> None:
    assert populated.list_files() == ["mod/a.py", "mod/b.py", "mod/d.py"]
    assert populated.list_files(prefix="mod/a") == ["mod/a.py"]
    assert populated.list_files(limit=1, offset=1) == ["mod/b.py"]
    assert populated.list_files(prefix="nope/") == []


def test_calls_edges_filters_kind(populated: DuckDBCodeGraphStore) -> None:
    edges = populated.calls_edges()
    assert ("mod.M", "mod.c") not in edges  # CONTAINS excluded
    assert len(edges) == 4


def test_centrality_roundtrip(populated: DuckDBCodeGraphStore) -> None:
    assert populated.write_centrality({"mod.a": 0.5, "mod.c": 0.9, "mod.b": 0.1}) == 3
    assert populated.read_centrality(["mod.a", "mod.c", "missing"]) == {
        "mod.a": pytest.approx(0.5),
        "mod.c": pytest.approx(0.9),
    }
    assert populated.read_centrality([]) == {}
    top = populated.top_centrality(limit=2)
    assert [qn for qn, _ in top] == ["mod.c", "mod.a"]
    # A rewrite replaces the snapshot wholesale.
    assert populated.write_centrality({"mod.b": 1.0}) == 1
    assert populated.read_centrality(["mod.a"]) == {}
    assert populated.top_centrality() == [("mod.b", pytest.approx(1.0))]


def test_content_hashes(populated: DuckDBCodeGraphStore) -> None:
    hashes = populated.content_hashes()
    assert hashes == {"mod.a": "hash-a", "mod.b": "hash-b", "mod.c": "hash-c"}
    # mod.d has no hash — must be absent, not None-valued.
    assert "mod.d" not in hashes


def test_meta_kv(store: DuckDBCodeGraphStore) -> None:
    assert store.get_meta("head_sha") is None
    store.set_meta("head_sha", "abc123")
    assert store.get_meta("head_sha") == "abc123"
    store.set_meta("head_sha", "def456")  # overwrite
    assert store.get_meta("head_sha") == "def456"


# The store holds a DOUBLED-prefix qualified_name; natural fqns must still resolve.
_DOUBLED = "agentalloy.src.agentalloy.api.proxy_signal.SignalResult"
_NATURAL = "agentalloy.api.proxy_signal.SignalResult"


class TestResolveQn:
    """`_resolve_qn` — exact match wins; else a UNIQUE dot-boundary suffix rescues
    a natural fqn against a doubled-prefix stored name (Bug A); ambiguity → miss."""

    def test_exact_match_is_returned_unchanged(self, store: DuckDBCodeGraphStore) -> None:
        store.upsert_symbols([sym(_DOUBLED, kind="Class", file_path="src/agentalloy/api/x.py")])
        assert store._resolve_qn(_DOUBLED) == _DOUBLED  # pyright: ignore[reportPrivateUsage]

    def test_natural_fqn_resolves_to_doubled(self, store: DuckDBCodeGraphStore) -> None:
        store.upsert_symbols([sym(_DOUBLED, kind="Class", file_path="src/agentalloy/api/x.py")])
        assert store._resolve_qn(_NATURAL) == _DOUBLED  # pyright: ignore[reportPrivateUsage]
        # ...and the public getter that natural-fqn callers hit works end to end.
        got = store.symbol(_NATURAL)
        assert got is not None and got.qualified_name == _DOUBLED

    def test_symbol_lookup_via_natural_fqn(self, store: DuckDBCodeGraphStore) -> None:
        store.upsert_symbols([sym("mod.a", file_path="mod/a.py")])
        # exact still works after the resolve prepend
        assert store.symbol("mod.a") is not None

    def test_governing_decisions_via_natural_fqn(self, store: DuckDBCodeGraphStore) -> None:
        store.upsert_symbols(
            [
                sym(_DOUBLED, kind="Class", file_path="src/agentalloy/api/proxy_signal.py"),
                sym(
                    "docs/design/x/approach.md::why",
                    kind="MarkdownDoc",
                    name="Why",
                    file_path="docs/design/x/approach.md",
                    start_line=3,
                    source_code=f"We shape `{_NATURAL}` this way.",
                ),
            ]
        )
        store.upsert_edges(
            [CodeEdge(src="docs/design/x/approach.md::why", dst=_DOUBLED, kind="GOVERNS")]
        )
        got = store.governing_decisions(_NATURAL)  # natural fqn, doubled store
        assert [d.qualified_name for d in got] == ["docs/design/x/approach.md::why"]

    def test_ambiguous_suffix_is_a_miss_not_a_wrong_pick(self, store: DuckDBCodeGraphStore) -> None:
        store.upsert_symbols(
            [
                sym("pkg.one.Thing", kind="Class", file_path="a.py"),
                sym("pkg.two.Thing", kind="Class", file_path="b.py"),
            ]
        )
        # Two names end with ".Thing" → unresolved (input unchanged) → symbol() miss.
        assert store._resolve_qn("Thing") == "Thing"  # pyright: ignore[reportPrivateUsage]
        assert store.symbol("Thing") is None

    def test_underscore_is_escaped_not_a_wildcard(self, store: DuckDBCodeGraphStore) -> None:
        # If `_` were an unescaped LIKE wildcard, "proxy_signal" would match BOTH
        # (ambiguous → miss). Escaped, only the literal-underscore name matches.
        store.upsert_symbols(
            [
                sym("a.b.proxy_signal", file_path="a.py"),
                sym("zzz.proxyXsignal", file_path="b.py"),
            ]
        )
        assert store._resolve_qn("proxy_signal") == "a.b.proxy_signal"  # pyright: ignore[reportPrivateUsage]

    def test_unknown_fqn_does_not_crash(self, store: DuckDBCodeGraphStore) -> None:
        store.upsert_symbols([sym("mod.a", file_path="mod/a.py")])
        assert store._resolve_qn("no.such.symbol") == "no.such.symbol"  # pyright: ignore[reportPrivateUsage]
        assert store.symbol("no.such.symbol") is None
        assert store.governing_decisions("no.such.symbol") == []
        assert store.callers("no.such.symbol") == []
