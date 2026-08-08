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


# -- #527 D: re-derive on symbol change, not only doc change --------------------


def test_rename_of_governed_symbol_re_derives_untouched_doc(
    store: DuckDBCodeGraphStore,
) -> None:
    """The deferred-loss mode (#527 failure mode 2), detected at the rename.

    A governed symbol is renamed and the decision doc is NOT edited. Pre-D,
    `affected` was doc-only, so nothing re-derived at all: no drop, no report,
    a dead edge left in the graph, and the loss deferred to whatever unrelated
    prose edit next touched the doc.

    Post-D the doc re-derives here. Its only span no longer resolves, so #527
    A's guard correctly keeps the stale edge rather than silently dropping it —
    and reports the doc suspicious, at the rename, where the context to fix it
    exists. Detection, not deletion, is the win.
    """
    store.upsert_symbols([sym("pkg.old_name", name="old_name")])
    doc = "docs/design/x/approach.md"
    c = chunk(f"{doc}::a", "We chose `pkg.old_name`.")
    _index_decisions(store, changed=[c], removed=[], chunks=[c])
    assert {d.qualified_name for d in store.governing_decisions("pkg.old_name")} == {f"{doc}::a"}

    # The rename: old symbol gone, new one in its place. The doc is untouched,
    # so it appears in neither `changed` nor `removed`.
    store.delete_for_files(["pkg/x.py"])
    store.upsert_symbols([sym("pkg.new_name", name="new_name")])

    # Pre-D control: with no code delta the doc is not reached at all.
    assert _index_decisions(store, changed=[], removed=[], chunks=[c]).suspicious_docs == []

    result = _index_decisions(
        store,
        changed=[],
        removed=[],
        chunks=[c],
        code_changed_qns={"pkg.old_name", "pkg.new_name"},
    )
    assert result.suspicious_docs == [doc]


def test_rename_drops_only_the_stale_edge_when_others_resolve(
    store: DuckDBCodeGraphStore,
) -> None:
    """Partial loss: the case #527 A's zero-edge guard cannot see.

    One of two governed symbols is renamed away. Re-derivation yields a
    non-empty edge set, so the suspicious guard does not fire — the stale edge
    is dropped and counted. Pre-D that drop happened at an unrelated later
    prose edit, far from its cause; now it happens at the rename.
    """
    store.upsert_symbols([sym("pkg.kept"), sym("pkg.renamed")])
    doc = "docs/design/x/approach.md"
    c = chunk(f"{doc}::a", "We chose `pkg.kept` over `pkg.renamed`.")
    _index_decisions(store, changed=[c], removed=[], chunks=[c])
    assert {d.qualified_name for d in store.governing_decisions("pkg.renamed")} == {f"{doc}::a"}

    store.delete_for_files(["pkg/x.py"])
    store.upsert_symbols([sym("pkg.kept")])  # `pkg.renamed` is gone

    result = _index_decisions(
        store, changed=[], removed=[], chunks=[c], code_changed_qns={"pkg.renamed"}
    )
    assert result.suspicious_docs == []  # non-empty derivation: guard silent
    assert result.dropped == 1
    assert store.governing_decisions("pkg.renamed") == []  # stale edge gone
    assert {d.qualified_name for d in store.governing_decisions("pkg.kept")} == {f"{doc}::a"}


def test_rename_relinks_when_the_span_still_resolves(store: DuckDBCodeGraphStore) -> None:
    """A tier-2 short-name span follows a move instead of being lost.

    The span text is unchanged prose; only the symbol's module moved. Tier 2
    resolves it again at the new FQN, so the edge is re-pointed rather than
    dropped — the outcome D exists to make possible. (`_handler` carries a
    leading underscore because tier 2 only resolves code-SHAPED spans; a plain
    word like `handler` is deliberately ignored.)
    """
    store.upsert_symbols([sym("pkg.a._handler", name="_handler")])
    doc = "docs/design/x/approach.md"
    c = chunk(f"{doc}::a", "The `_handler` owns this.")
    _index_decisions(store, changed=[c], removed=[], chunks=[c])
    assert {d.qualified_name for d in store.governing_decisions("pkg.a._handler")} == {f"{doc}::a"}

    # Same short name, moved to another module — the span still resolves.
    store.delete_for_files(["pkg/x.py"])
    store.upsert_symbols([sym("pkg.b._handler", name="_handler")])
    _index_decisions(
        store,
        changed=[],
        removed=[],
        chunks=[c],
        code_changed_qns={"pkg.a._handler", "pkg.b._handler"},
    )

    assert store.governing_decisions("pkg.a._handler") == []
    assert {d.qualified_name for d in store.governing_decisions("pkg.b._handler")} == {f"{doc}::a"}


def test_unrelated_code_change_does_not_touch_decision_docs(
    store: DuckDBCodeGraphStore,
) -> None:
    """Only docs that actually govern the changed symbols are re-derived."""
    store.upsert_symbols([sym("pkg.foo"), sym("pkg.unrelated")])
    doc = "docs/design/x/approach.md"
    c = chunk(f"{doc}::a", "We chose `pkg.foo`.")
    _index_decisions(store, changed=[c], removed=[], chunks=[c])

    result = _index_decisions(
        store, changed=[], removed=[], chunks=[c], code_changed_qns={"pkg.unrelated"}
    )
    assert result.written == 0
    assert result.dropped == 0
    assert result.suspicious_docs == []
    assert {d.qualified_name for d in store.governing_decisions("pkg.foo")} == {f"{doc}::a"}


def test_symbol_affected_doc_with_no_chunks_is_not_re_derived(
    store: DuckDBCodeGraphStore,
) -> None:
    """A doc with no current chunks must not be dragged in by a code change.

    It would derive zero edges, trip the #527 A suspicious guard, and raise a
    false alarm on every unrelated code change — degrading B's signal quality.
    """
    store.upsert_symbols([sym("pkg.foo")])
    doc = "docs/design/x/approach.md"
    c = chunk(f"{doc}::a", "We chose `pkg.foo`.")
    _index_decisions(store, changed=[c], removed=[], chunks=[c])

    result = _index_decisions(
        store, changed=[], removed=[], chunks=[], code_changed_qns={"pkg.foo"}
    )
    assert result.suspicious_docs == []
    assert result.dropped == 0
    assert {d.qualified_name for d in store.governing_decisions("pkg.foo")} == {f"{doc}::a"}


def test_decision_docs_governing_finds_renamed_away_fqn(store: DuckDBCodeGraphStore) -> None:
    """The store query must not join through `symbols`.

    `decisions_for_files` and `governing_decisions` both join `symbols` on the
    edge's dst, so a renamed-away FQN is invisible to them — precisely the row
    D needs. This pins the no-join contract.
    """
    store.upsert_symbols([sym("pkg.gone")])
    doc = "docs/design/x/approach.md"
    c = chunk(f"{doc}::a", "We chose `pkg.gone`.")
    _index_decisions(store, changed=[c], removed=[], chunks=[c])

    store.delete_for_files(["pkg/x.py"])  # symbol row deleted; GOVERNS edge kept
    assert store.symbol("pkg.gone") is None
    assert store.decisions_for_files(["pkg/x.py"]) == []  # join-based: blind to it
    assert store.decision_docs_governing(["pkg.gone"]) == [doc]


def test_decision_docs_governing_empty_input(store: DuckDBCodeGraphStore) -> None:
    assert store.decision_docs_governing([]) == []
