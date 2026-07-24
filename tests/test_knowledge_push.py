"""knowledge_push.build_decision_block — the slice-2 selection/render helper.

Pure logic given an opened graph store + the already-composed tier-2 text:
resolve scope.touches → governed decisions, dedup against composition (DK4),
inert superseded filter (DK5), cap (DK6), render (DK7).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from agentalloy.api import knowledge_push
from agentalloy.api.knowledge_push import build_decision_block
from agentalloy.code_index.store.graph_store import DuckDBCodeGraphStore
from agentalloy.contracts import Contract, ContractScope
from agentalloy.storage.protocols import CodeEdge, CodeSymbol


def contract(touches: list[str]) -> Contract:
    return Contract(
        path=Path("c.md"),
        phase="design",
        task_slug="t",
        domain_tags=[],
        scope=ContractScope(touches=touches, avoids=[]),
        success_criteria=[],
        related_contracts=[],
        created_at=None,
        body="",
        route="full",
    )


def code_sym(qn: str, file_path: str) -> CodeSymbol:
    return CodeSymbol(
        qualified_name=qn,
        kind="Function",
        name=qn.rsplit(".", 1)[-1],
        file_path=file_path,
        start_line=1,
        end_line=5,
        docstring=None,
        decorators=[],
        is_exported=None,
        is_async=False,
        is_generator=False,
        source_code=None,
    )


def decision_sym(qn: str, heading: str, body: str) -> CodeSymbol:
    return CodeSymbol(
        qualified_name=qn,
        kind="MarkdownDoc",
        name=heading,
        file_path=qn.split("::")[0],
        start_line=3,
        end_line=9,
        docstring=None,
        decorators=[],
        is_exported=None,
        is_async=False,
        is_generator=False,
        source_code=body,
    )


def governs(src: str, dst: str) -> CodeEdge:
    return CodeEdge(src=src, dst=dst, kind="GOVERNS", file_path=src.split("::")[0])


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBCodeGraphStore]:
    s = DuckDBCodeGraphStore(tmp_path / "graph.duck")
    s.migrate()
    yield s
    s.close()


def _seed_one(store: DuckDBCodeGraphStore, decision_qn: str) -> None:
    store.upsert_symbols(
        [
            code_sym("pkg.a.foo", "pkg/a.py"),
            decision_sym(decision_qn, "Why foo", "Chose `pkg.a.foo`."),
        ]
    )
    store.upsert_edges([governs(decision_qn, "pkg.a.foo")])


def test_push_present_for_governed_touch(store: DuckDBCodeGraphStore) -> None:
    _seed_one(store, "docs/design/x/approach.md::why-foo")
    push = build_decision_block(contract(["pkg/a.py"]), "", store)
    assert push is not None
    assert push.count == 1 and push.truncated is False
    assert "# Decisions governing this work" in push.text
    assert "Why foo" in push.text and "Chose `pkg.a.foo`." in push.text
    assert "docs/design/x/approach.md" in push.text


def test_snippet_leading_heading_not_duplicated(store: DuckDBCodeGraphStore) -> None:
    # Real markdown chunks carry their own heading line in the body (UAT finding:
    # "## Why token bucket" rendered twice). _render must drop the snippet's
    # leading heading when it duplicates the one it just emitted.
    store.upsert_symbols(
        [
            code_sym("pkg.a.foo", "pkg/a.py"),
            decision_sym(
                "docs/design/x/approach.md::why-foo",
                "Why foo",
                "## Why foo\n\nChose `pkg.a.foo`.",
            ),
        ]
    )
    store.upsert_edges([governs("docs/design/x/approach.md::why-foo", "pkg.a.foo")])
    push = build_decision_block(contract(["pkg/a.py"]), "", store)
    assert push is not None
    assert push.text.count("Why foo") == 1
    assert "Chose `pkg.a.foo`." in push.text


def test_none_when_no_touches_or_no_decisions(store: DuckDBCodeGraphStore) -> None:
    _seed_one(store, "docs/design/x/approach.md::why-foo")
    assert build_decision_block(contract([]), "", store) is None  # no scope
    assert build_decision_block(contract(["pkg/z.py"]), "", store) is None  # ungoverned file


def test_defers_only_when_promoted_fragment_in_composed_text(store: DuckDBCodeGraphStore) -> None:
    # a solutions-sourced decision -> slug "foo" -> skill_id "foo-lesson"
    _seed_one(store, "docs/solutions/foo.md::d")
    # promoted skill present in this turn's composed text -> defer -> None
    composed = (
        "# Domain fragments\n\n## skill: foo-lesson\n\n### rationale — foo-lesson-v1-f1\nwhy\n"
    )
    assert build_decision_block(contract(["pkg/a.py"]), composed, store) is None
    # skill absent from composed text -> pushed (the D1 no-silent-gap case)
    push = build_decision_block(contract(["pkg/a.py"]), "## skill: something-else\n", store)
    assert push is not None and push.count == 1


def test_approach_md_never_deferred(store: DuckDBCodeGraphStore) -> None:
    _seed_one(store, "docs/design/x/approach.md::why-foo")
    # even with a -lesson skill in composed text, an approach.md decision pushes
    push = build_decision_block(contract(["pkg/a.py"]), "## skill: why-foo-lesson\n", store)
    assert push is not None and push.count == 1


def test_superseded_filter_is_wired(
    store: DuckDBCodeGraphStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_one(store, "docs/design/x/approach.md::why-foo")
    monkeypatch.setattr(knowledge_push, "_is_superseded", lambda d: True)
    assert build_decision_block(contract(["pkg/a.py"]), "", store) is None


def test_caps_and_truncation(store: DuckDBCodeGraphStore) -> None:
    store.upsert_symbols([code_sym("pkg.a.foo", "pkg/a.py")])
    n = knowledge_push._MAX_DECISIONS + 3
    for i in range(n):
        qn = f"docs/design/d{i:02d}/approach.md::d"
        store.upsert_symbols([decision_sym(qn, f"Decision {i}", "Governs `pkg.a.foo`.")])
        store.upsert_edges([governs(qn, "pkg.a.foo")])
    push = build_decision_block(contract(["pkg/a.py"]), "", store)
    assert push is not None
    assert push.count == knowledge_push._MAX_DECISIONS and push.truncated is True


def test_phase2_related_decisions(
    store: DuckDBCodeGraphStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 2: related_decisions merges thematic decisions and sets related_count."""
    # Seed a governed decision
    _seed_one(store, "docs/design/x/approach.md::why-foo")

    from agentalloy.code_index.retrieval.hybrid import SearchResult

    related = [
        SearchResult(
            qualified_name="docs/solutions/bar.md::thematic-bar",
            kind="MarkdownDoc",
            file_path="docs/solutions/bar.md",
            start_line=1,
            end_line=10,
            snippet="Chose bar for reason X.",
            score=0.85,
        ),
    ]

    # related_decisions is async; asyncio.run() needs a coroutine
    async def _mock(*a, **kw):
        return related

    monkeypatch.setattr(
        "agentalloy.code_index.retrieval.hybrid.related_decisions",
        _mock,
        raising=False,
    )

    push = build_decision_block(
        contract(["pkg/a.py"]),
        "",
        store,
        state={},  # non-None triggers phase 2
        slug="my-task",
        task_title="Implement foo with bar",
    )
    assert push is not None
    # Should include both the governed decision AND the related one
    assert push.count == 2
    assert push.related_count == 1
    assert "Why foo" in push.text
    assert "thematic-bar" in push.text  # heading derived from qualified_name anchor
    assert "Chose `pkg.a.foo`." in push.text
    assert "Chose bar for reason X." in push.text


def test_phase2_no_related_when_params_missing(
    store: DuckDBCodeGraphStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 2 is skipped when any of state/slug/task_title is missing."""
    _seed_one(store, "docs/design/x/approach.md::why-foo")

    # Track that related_decisions is NOT called
    called = []

    async def _mock(*a, **kw):
        called.append(1)
        return []

    monkeypatch.setattr(
        "agentalloy.code_index.retrieval.hybrid.related_decisions",
        _mock,
        raising=False,
    )

    # state=None -> phase 2 skipped
    push = build_decision_block(contract(["pkg/a.py"]), "", store)
    assert push is not None
    assert push.count == 1
    assert push.related_count == 0
    assert len(called) == 0

    # slug=None -> phase 2 skipped
    push = build_decision_block(
        contract(["pkg/a.py"]), "", store, state={}, slug=None, task_title="x"
    )
    assert push is not None
    assert push.count == 1
    assert len(called) == 0

    # task_title=None -> phase 2 skipped
    push = build_decision_block(
        contract(["pkg/a.py"]), "", store, state={}, slug="x", task_title=None
    )
    assert push is not None
    assert push.count == 1
    assert len(called) == 0


def test_phase2_related_dedup_keeps_governed(
    store: DuckDBCodeGraphStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When related_decisions returns a decision already in the governed set, it is deduped."""
    _seed_one(store, "docs/design/x/approach.md::why-foo")

    from agentalloy.code_index.retrieval.hybrid import SearchResult

    related = [
        SearchResult(
            qualified_name="docs/design/x/approach.md::why-foo",
            kind="MarkdownDoc",
            file_path="docs/design/x/approach.md",
            start_line=3,
            end_line=9,
            snippet="Chose `pkg.a.foo`.",
            score=0.90,
        ),
    ]

    async def _mock(*a, **kw):
        return related

    monkeypatch.setattr(
        "agentalloy.code_index.retrieval.hybrid.related_decisions",
        _mock,
        raising=False,
    )

    push = build_decision_block(
        contract(["pkg/a.py"]),
        "",
        store,
        state={},
        slug="my-task",
        task_title="Implement foo",
    )
    assert push is not None
    # The governed decision is kept once; related_count should be 0 (deduped)
    assert push.count == 1
    assert push.related_count == 0


def test_phase2_graceful_degradation_on_failure(
    store: DuckDBCodeGraphStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When related_decisions raises, the function falls back to governed-only."""
    _seed_one(store, "docs/design/x/approach.md::why-foo")

    async def _mock(*a, **kw):
        raise RuntimeError("no index")

    monkeypatch.setattr(
        "agentalloy.code_index.retrieval.hybrid.related_decisions",
        _mock,
        raising=False,
    )

    push = build_decision_block(
        contract(["pkg/a.py"]),
        "",
        store,
        state={},
        slug="my-task",
        task_title="Implement foo",
    )
    # Should still return the governed decision (graceful degradation)
    assert push is not None
    assert push.count == 1
    assert push.related_count == 0
    assert "Why foo" in push.text
