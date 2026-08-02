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
    result = _extract_governed_symbols(body, store)
    assert {fqn for fqn, _span, _tier in result.governed} == {
        "pkg.mod.foo",
        "pkg.helpers._do_thing",
    }
    tiers = {fqn: tier for fqn, _span, tier in result.governed}
    assert tiers["pkg.mod.foo"] == 1  # exact-fqn match
    assert tiers["pkg.helpers._do_thing"] == 2  # unique short-name match
    assert result.unresolved == []


def test_extract_drops_ambiguous_word_and_markdown(store: DuckDBCodeGraphStore) -> None:
    store.upsert_symbols(
        [
            sym("pkg.a._dup", name="_dup"),
            sym("pkg.b._dup", name="_dup"),  # ambiguous: two match
            sym("pkg.run", name="run"),  # matches a bare English word
            sym("docs/x.md::sec", kind="MarkdownDoc", name="sec"),  # a doc chunk
        ]
    )
    # `_dup` is code-shaped and ambiguous -> reported unresolved, not silently
    # dropped. `run` not code-shaped -> drop; the md chunk (exact fqn) is
    # MarkdownDoc -> excluded; `pipeline.py` resolves to nothing -> drop.
    body = "Touches `_dup`, calls `run`, see `docs/x.md::sec` and `pipeline.py`."
    result = _extract_governed_symbols(body, store)
    assert result.governed == []
    assert result.unresolved == ["_dup"]


def test_extract_ambiguity_flip_reports_unresolved_not_silent_drop(
    store: DuckDBCodeGraphStore,
) -> None:
    """A span that resolves cleanly (tier 2) must flip to reported-unresolved
    the moment a second same-named symbol appears — never a silent drop."""
    store.upsert_symbols([sym("pkg.a._helper", name="_helper")])
    body = "See `_helper`."
    result = _extract_governed_symbols(body, store)
    assert [fqn for fqn, _span, _tier in result.governed] == ["pkg.a._helper"]
    assert result.unresolved == []

    # A second symbol with the same short name lands (e.g. a sibling file).
    store.upsert_symbols([sym("pkg.b._helper", name="_helper")])
    result = _extract_governed_symbols(body, store)
    assert result.governed == []
    assert result.unresolved == ["_helper"]  # ambiguity surfaced, not dropped


# -- DK6: doc-granular re-derive, incl. AC 3 sibling survival ------------------


def test_index_decisions_links_and_survives_sibling_removal(store: DuckDBCodeGraphStore) -> None:
    store.upsert_symbols([sym("pkg.foo"), sym("pkg._bar", name="_bar")])
    doc = "docs/design/x/approach.md"
    a = chunk(f"{doc}::a", "We chose `pkg.foo`.")
    b = chunk(f"{doc}::b", "And `_bar` here.")  # short-name (tier-2) span

    # initial index: both decisions link
    _index_decisions(store, changed=[a, b], removed=[], chunks=[a, b])
    assert {d.qualified_name for d in store.governing_decisions("pkg.foo")} == {f"{doc}::a"}
    assert {d.qualified_name for d in store.governing_decisions("pkg._bar")} == {f"{doc}::b"}
    # Provenance (#527 C) round-trips: tier-1 exact fqn vs tier-2 short-name,
    # and the SPAN stored is the fenced text, not the resolved fqn.
    row_a = store.conn.execute(
        "SELECT span, resolution_tier FROM edges WHERE kind='GOVERNS' AND dst='pkg.foo'"
    ).fetchone()
    assert row_a == ("pkg.foo", 1)
    row_b = store.conn.execute(
        "SELECT span, resolution_tier FROM edges WHERE kind='GOVERNS' AND dst='pkg._bar'"
    ).fetchone()
    assert row_b == ("_bar", 2)

    # chunk a is removed from the same doc; b is unchanged. The doc-granular
    # re-derive must restore b's link, not drop it (the AC 3 fix).
    _index_decisions(store, changed=[], removed=[f"{doc}::a"], chunks=[b])
    assert store.governing_decisions("pkg.foo") == []  # a's link pruned
    assert {d.qualified_name for d in store.governing_decisions("pkg._bar")} == {f"{doc}::b"}


def test_index_decisions_ignores_non_source_docs(store: DuckDBCodeGraphStore) -> None:
    store.upsert_symbols([sym("pkg.foo")])
    c = chunk("docs/notes/random.md::x", "Mentions `pkg.foo`.")
    _index_decisions(store, changed=[c], removed=[], chunks=[c])
    assert store.governing_decisions("pkg.foo") == []  # not a decision source


# -- #527 A: derive-first/swap-second guard -------------------------------------


def test_zero_derived_edges_where_edges_existed_keeps_them_and_reports_suspicious(
    store: DuckDBCodeGraphStore,
) -> None:
    """A doc that still has current chunks but whose re-derivation collapses
    to zero edges (e.g. the governed symbol itself vanished) must not
    delete-then-fail-to-write: the prior edges survive and the doc is
    reported suspicious."""
    store.upsert_symbols([sym("pkg.foo"), sym("pkg.bar")])
    doc = "docs/design/x/approach.md"
    a = chunk(f"{doc}::a", "We chose `pkg.foo`.")
    result = _index_decisions(store, changed=[a], removed=[], chunks=[a])
    assert result.written == 1
    assert {d.qualified_name for d in store.governing_decisions("pkg.foo")} == {f"{doc}::a"}

    # Re-index the SAME doc, but now its body no longer resolves to anything
    # (the symbol it named is gone, or the wording changed) — chunk `a` is
    # still present (doc not wholesale removed), yet extraction yields zero.
    a_no_match = chunk(f"{doc}::a", "This mentions nothing resolvable.")
    result2 = _index_decisions(store, changed=[a_no_match], removed=[], chunks=[a_no_match])
    assert result2.written == 0
    assert result2.dropped == 0
    assert result2.suspicious_docs == [doc]
    # The prior edge survives — not deleted.
    assert {d.qualified_name for d in store.governing_decisions("pkg.foo")} == {f"{doc}::a"}


def test_delta_reporting_written_and_dropped_counts(store: DuckDBCodeGraphStore) -> None:
    store.upsert_symbols([sym("pkg.foo"), sym("pkg.bar")])
    doc = "docs/design/x/approach.md"
    a = chunk(f"{doc}::a", "We chose `pkg.foo`.")
    b = chunk(f"{doc}::b", "And `pkg.bar` here.")
    first = _index_decisions(store, changed=[a, b], removed=[], chunks=[a, b])
    assert first.written == 2
    assert first.dropped == 0

    # b's wording drops its link; a survives unchanged content-wise but is
    # still re-derived (doc-granular). Net: 2 prior edges -> 1 new edge.
    b_gone = chunk(f"{doc}::b", "No governed symbol mentioned here anymore.")
    second = _index_decisions(store, changed=[a, b_gone], removed=[], chunks=[a, b_gone])
    assert second.written == 1
    assert second.dropped == 1  # 2 prior - 1 written


# -- #527 escape hatch: --prune-decisions gates wholesale doc removal ----------


def test_wholesale_removed_doc_retains_edges_without_prune_flag(
    store: DuckDBCodeGraphStore,
) -> None:
    store.upsert_symbols([sym("pkg.foo")])
    doc = "docs/design/x/approach.md"
    a = chunk(f"{doc}::a", "We chose `pkg.foo`.")
    _index_decisions(store, changed=[a], removed=[], chunks=[a])
    assert store.governing_decisions("pkg.foo") != []

    # The doc is now wholesale gone: no chunk for it anywhere in `chunks`.
    result = _index_decisions(
        store, changed=[], removed=[f"{doc}::a"], chunks=[], prune_decisions=False
    )
    assert result.written == 0
    assert result.dropped == 0
    assert store.governing_decisions("pkg.foo") != []  # retained


def test_wholesale_removed_doc_dropped_with_prune_flag(store: DuckDBCodeGraphStore) -> None:
    store.upsert_symbols([sym("pkg.foo")])
    doc = "docs/design/x/approach.md"
    a = chunk(f"{doc}::a", "We chose `pkg.foo`.")
    _index_decisions(store, changed=[a], removed=[], chunks=[a])
    assert store.governing_decisions("pkg.foo") != []

    result = _index_decisions(
        store, changed=[], removed=[f"{doc}::a"], chunks=[], prune_decisions=True
    )
    assert result.written == 0
    assert result.dropped == 1
    assert store.governing_decisions("pkg.foo") == []  # actually removed
