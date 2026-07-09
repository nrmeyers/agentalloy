"""_index_decisions ingest phase (build 02): decision→symbol linkage.

Covers DK2 (backtick-span resolution + code-shaped guard), DK5 (source
allow-list), and DK6 (doc-granular re-derive — the AC 3 sibling-survival fix).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from agentalloy.code_index.ingest.markdown import MarkdownChunk
from agentalloy.code_index.ingest.pipeline import (
    _extract_governed_symbols,
    _index_decisions,
    _is_code_shaped,
    _is_decision_source,
)
from agentalloy.code_index.store.graph_store import DuckDBCodeGraphStore
from agentalloy.storage.protocols import CodeSymbol


def sym(qn: str, *, kind: str = "Function", name: str | None = None) -> CodeSymbol:
    return CodeSymbol(
        qualified_name=qn,
        kind=kind,
        name=name if name is not None else qn.rsplit(".", 1)[-1],
        file_path=(qn.split("::")[0] if "::" in qn else "pkg/x.py"),
        start_line=1,
        end_line=5,
        docstring=None,
        decorators=[],
        is_exported=None,
        is_async=False,
        is_generator=False,
        source_code=None,
    )


def chunk(qn: str, body: str, *, heading: str = "Why") -> MarkdownChunk:
    return MarkdownChunk(
        qualified_name=qn,
        file_path=qn.split("::")[0],
        heading=heading,
        body=body,
        start_line=1,
        end_line=9,
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBCodeGraphStore]:
    s = DuckDBCodeGraphStore(tmp_path / "graph.duck")
    s.migrate()
    yield s
    s.close()


# -- DK5: decision-source allow-list ------------------------------------------


@pytest.mark.parametrize(
    "path,expected",
    [
        ("docs/solutions/foo.md", True),
        ("docs/design/foo/approach.md", True),
        ("docs/spec-contracts/foo.design/approach.md", True),
        ("docs/design/foo/tasks.md", False),  # not approach.md
        ("docs/ship/foo.md", False),  # ship excluded by default
        ("docs/qa/foo.md", False),
        ("README.md", False),
        ("src/agentalloy/x.md", False),
    ],
)
def test_is_decision_source(path: str, expected: bool) -> None:
    assert _is_decision_source(path) is expected


# -- DK2: code-shaped guard ----------------------------------------------------


@pytest.mark.parametrize(
    "span,expected",
    [
        ("run", False),  # bare English word
        ("build", False),
        ("config", False),
        ("_index_markdown", True),  # internal underscore
        ("DecisionRow", True),  # internal caps
        ("pkg.foo", True),  # dotted
        ("a/b.py", True),  # path sep
        ("Cls::method", True),  # scope sep
        ("get user", False),  # space -> not an identifier
        ("", False),
    ],
)
def test_is_code_shaped(span: str, expected: bool) -> None:
    assert _is_code_shaped(span) is expected


# -- DK2: linkage resolution ---------------------------------------------------


def test_extract_exact_fqn_and_unambiguous_name(store: DuckDBCodeGraphStore) -> None:
    store.upsert_symbols(
        [
            sym("pkg.mod.foo"),  # exact-fqn target
            sym("pkg.helpers._do_thing", name="_do_thing"),  # unique short name
        ]
    )
    body = "We route through `pkg.mod.foo` via the `_do_thing` helper."
    assert _extract_governed_symbols(body, store) == {"pkg.mod.foo", "pkg.helpers._do_thing"}


def test_extract_drops_ambiguous_word_and_markdown(store: DuckDBCodeGraphStore) -> None:
    store.upsert_symbols(
        [
            sym("pkg.a._dup", name="_dup"),
            sym("pkg.b._dup", name="_dup"),  # ambiguous: two match
            sym("pkg.run", name="run"),  # matches a bare English word
            sym("docs/x.md::sec", kind="MarkdownDoc", name="sec"),  # a doc chunk
        ]
    )
    # `_dup` ambiguous -> drop; `run` not code-shaped -> drop; the md chunk (exact
    # fqn) is MarkdownDoc -> excluded; `pipeline.py` resolves to nothing.
    body = "Touches `_dup`, calls `run`, see `docs/x.md::sec` and `pipeline.py`."
    assert _extract_governed_symbols(body, store) == set()


# -- DK6: doc-granular re-derive, incl. AC 3 sibling survival ------------------


def test_index_decisions_links_and_survives_sibling_removal(store: DuckDBCodeGraphStore) -> None:
    store.upsert_symbols([sym("pkg.foo"), sym("pkg.bar")])
    doc = "docs/design/x/approach.md"
    a = chunk(f"{doc}::a", "We chose `pkg.foo`.")
    b = chunk(f"{doc}::b", "And `pkg.bar` here.")

    # initial index: both decisions link
    _index_decisions(store, changed=[a, b], removed=[], chunks=[a, b])
    assert {d.qualified_name for d in store.governing_decisions("pkg.foo")} == {f"{doc}::a"}
    assert {d.qualified_name for d in store.governing_decisions("pkg.bar")} == {f"{doc}::b"}

    # chunk a is removed from the same doc; b is unchanged. The doc-granular
    # re-derive must restore b's link, not drop it (the AC 3 fix).
    _index_decisions(store, changed=[], removed=[f"{doc}::a"], chunks=[b])
    assert store.governing_decisions("pkg.foo") == []  # a's link pruned
    assert {d.qualified_name for d in store.governing_decisions("pkg.bar")} == {f"{doc}::b"}


def test_index_decisions_ignores_non_source_docs(store: DuckDBCodeGraphStore) -> None:
    store.upsert_symbols([sym("pkg.foo")])
    c = chunk("docs/notes/random.md::x", "Mentions `pkg.foo`.")
    _index_decisions(store, changed=[c], removed=[], chunks=[c])
    assert store.governing_decisions("pkg.foo") == []  # not a decision source
