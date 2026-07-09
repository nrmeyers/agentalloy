"""GOVERNS-edge store primitives for the Knowledge module (build 01).

Covers: symbols_by_name (DK2 tier-2 lookup), governing_decisions (AC 5 read),
delete_govern_edges_for_doc (DK6 doc-granular prune), and a read-back of a
decision + its governed symbol with no schema change (AC 1).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from agentalloy.code_index.store.graph_store import DuckDBCodeGraphStore
from agentalloy.storage.protocols import CodeEdge, CodeSymbol, DecisionRow


def sym(
    qn: str, *, kind: str = "Function", file_path: str | None = None, **kw: object
) -> CodeSymbol:
    defaults: dict[str, object] = {
        "name": qn.rsplit(".", 1)[-1].rsplit("::", 1)[-1],
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


def governs(src: str, dst: str, *, doc: str) -> CodeEdge:
    return CodeEdge(src=src, dst=dst, kind="GOVERNS", file_path=doc)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBCodeGraphStore]:
    s = DuckDBCodeGraphStore(tmp_path / "graph.duck")
    s.migrate()
    yield s
    s.close()


# -- AC 1: a decision + its governed symbol round-trips with no DDL -----------


def test_governing_decisions_reads_back_decision_and_link(store: DuckDBCodeGraphStore) -> None:
    store.upsert_symbols(
        [
            sym("pkg.foo", file_path="pkg/foo.py"),
            sym(
                "docs/design/x/approach.md::why-foo",
                kind="MarkdownDoc",
                name="Why foo",
                file_path="docs/design/x/approach.md",
                start_line=12,
                source_code="We chose `pkg.foo` deliberately.",
            ),
        ]
    )
    store.upsert_edges(
        [governs("docs/design/x/approach.md::why-foo", "pkg.foo", doc="docs/design/x/approach.md")]
    )

    got = store.governing_decisions("pkg.foo")

    assert got == [
        DecisionRow(
            qualified_name="docs/design/x/approach.md::why-foo",
            file_path="docs/design/x/approach.md",
            start_line=12,
            heading="Why foo",
            snippet="We chose `pkg.foo` deliberately.",
        )
    ]


def test_governing_decisions_empty_for_ungoverned(store: DuckDBCodeGraphStore) -> None:
    store.upsert_symbols([sym("pkg.foo", file_path="pkg/foo.py")])
    assert store.governing_decisions("pkg.foo") == []
    assert store.governing_decisions("does.not.exist") == []


# -- symbols_by_name: DK2 tier-2 lookup, MarkdownDoc excluded -----------------


def test_symbols_by_name_returns_code_symbols_only(store: DuckDBCodeGraphStore) -> None:
    store.upsert_symbols(
        [
            sym("pkg.a.run", name="run", file_path="pkg/a.py"),
            sym("pkg.b.run", name="run", file_path="pkg/b.py"),
            sym("docs/x.md::run", kind="MarkdownDoc", name="run", file_path="docs/x.md"),
            sym("pkg.solo", name="solo", file_path="pkg/s.py"),
        ]
    )

    multi = store.symbols_by_name("run")
    assert {qn for qn, _ in multi} == {"pkg.a.run", "pkg.b.run"}  # MarkdownDoc excluded
    assert all(kind != "MarkdownDoc" for _, kind in multi)

    assert store.symbols_by_name("solo") == [("pkg.solo", "Function")]
    assert store.symbols_by_name("missing") == []


# -- DK6: doc-granular prune ---------------------------------------------------


def test_delete_govern_edges_for_doc_is_scoped(store: DuckDBCodeGraphStore) -> None:
    store.upsert_symbols([sym("pkg.foo", file_path="pkg/foo.py")])
    store.upsert_edges(
        [
            governs("docs/a.md::d1", "pkg.foo", doc="docs/a.md"),
            governs("docs/a.md::d2", "pkg.foo", doc="docs/a.md"),
            governs("docs/b.md::d3", "pkg.foo", doc="docs/b.md"),
            CodeEdge(src="pkg.x", dst="pkg.foo", kind="CALLS", file_path="docs/a.md"),
        ]
    )

    removed = store.delete_govern_edges_for_doc("docs/a.md")

    assert removed == 2  # only docs/a.md's two GOVERNS edges
    # docs/b.md GOVERNS survives; the CALLS edge (even sharing docs/a.md) survives
    assert {d.qualified_name for d in store.governing_decisions("pkg.foo")} == {"docs/b.md::d3"}
    assert any(c.qualified_name == "pkg.x" for c in store.callers("pkg.foo"))
