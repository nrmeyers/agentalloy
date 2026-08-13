"""retrieval.bundle — expansion reasons, test-path penalty, budget truncation."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from agentalloy.code_index.api.state import CodeIndexState
from agentalloy.code_index.retrieval.bundle import build_bundle
from agentalloy.code_index.store import open_jobs
from agentalloy.config import Settings
from agentalloy.storage.protocols import CodeEdge

from .conftest import FixedEmbedClient, axis_vec, calls_edge, make_symbol, seed_index, vector_row


def _governs_edge(src: str, dst: str) -> CodeEdge:
    return CodeEdge(src=src, dst=dst, kind="GOVERNS", file_path=src.split("::")[0])

SLUG = "repo"


@pytest.fixture
def state(settings: Settings) -> Iterator[CodeIndexState]:
    st = CodeIndexState(
        settings=settings, embed_client=FixedEmbedClient(axis_vec(0)), jobs=open_jobs(settings)
    )
    yield st
    st.jobs.close()


def seed_call_graph(settings: Settings, slug: str = SLUG) -> None:
    """One strong seed (pkg.core) with a caller and a callee that have graph
    rows + edges but NO vector rows — they are reachable only via expansion."""
    seed_index(
        settings,
        slug,
        symbols=[
            make_symbol("pkg.core", source_code="def core():\n    return leaf()"),
            make_symbol("pkg.entry", source_code="def entry():\n    return core()"),
            make_symbol("pkg.leaf", source_code="def leaf():\n    return 42"),
        ],
        edges=[calls_edge("pkg.entry", "pkg.core"), calls_edge("pkg.core", "pkg.leaf")],
        vectors=[vector_row("pkg.core", axis_vec(0))],
    )


async def test_expansion_includes_callers_and_callees(state: CodeIndexState) -> None:
    seed_call_graph(state.settings)
    bundle = await build_bundle(state, SLUG, "explain the core routine")
    by_qn = {item.qualified_name: item for item in bundle.items}

    assert by_qn["pkg.core"].reason == "seed"
    assert by_qn["pkg.entry"].reason == "caller"
    assert by_qn["pkg.leaf"].reason == "callee"
    # Neighbours inherit a decayed fraction of the seed score.
    assert by_qn["pkg.entry"].score == pytest.approx(by_qn["pkg.core"].score * 0.5)
    assert by_qn["pkg.leaf"].score == pytest.approx(by_qn["pkg.core"].score * 0.5)
    # Seed source is included; totals line up with the header+source costs.
    assert "def core()" in by_qn["pkg.core"].source
    assert bundle.total_chars <= bundle.budget_chars
    assert bundle.seed_count == 1


async def test_test_path_penalty_demotes_test_symbol(state: CodeIndexState) -> None:
    """Equal-rank-adjacent seeds: the test-path one drops below production."""
    seed_index(
        state.settings,
        SLUG,
        symbols=[
            make_symbol("pkg.tests.test_core", file_path="pkg/tests/test_core.py"),
            make_symbol("pkg.impl"),
        ],
        vectors=[
            # The test symbol has the HIGHER cosine — without the penalty it
            # would rank first.
            vector_row("pkg.tests.test_core", axis_vec(0), file_path="pkg/tests/test_core.py"),
            vector_row("pkg.impl", axis_vec(0, 1)),
        ],
    )
    bundle = await build_bundle(state, SLUG, "core behaviour")
    names = [item.qualified_name for item in bundle.items]
    assert names.index("pkg.impl") < names.index("pkg.tests.test_core")
    by_qn = {item.qualified_name: item for item in bundle.items}
    assert by_qn["pkg.tests.test_core"].score < by_qn["pkg.impl"].score


async def test_budget_truncation(state: CodeIndexState) -> None:
    seed_call_graph(state.settings)
    full = await build_bundle(state, SLUG, "explain the core routine", budget_chars=24000)
    assert len(full.items) == 3

    small = await build_bundle(state, SLUG, "explain the core routine", budget_chars=500)
    # 500 is enough for the whole tiny fixture; shrink via the floor instead:
    # each item costs len(qn) + len(file_path) + 24 header chars + source.
    tight_budget = 60  # room for roughly one header + a sliver of source
    # build_bundle is not exposed below the request-model floor via HTTP, but
    # the function itself honours any budget.
    tight = await build_bundle(state, SLUG, "explain the core routine", budget_chars=tight_budget)
    assert len(tight.items) < len(full.items)
    assert tight.total_chars <= tight_budget
    # Headers always present on every included item.
    for item in tight.items:
        assert item.qualified_name
        assert item.file_path
        assert item.start_line is not None
    assert small.total_chars <= 500


async def test_bundle_includes_governing_decisions(state: CodeIndexState) -> None:
    """Bundle items carry governing decisions — the 'why' travels with the code."""
    from agentalloy.storage.protocols import CodeSymbol

    decision_qn = "docs/design/x/approach.md::why-core"
    seed_index(
        state.settings,
        SLUG,
        symbols=[
            make_symbol("pkg.core", source_code="def core():\n    return 42"),
            # Decision symbol — name is the heading (as the real pipeline sets it)
            CodeSymbol(
                qualified_name=decision_qn,
                kind="MarkdownDoc",
                name="Why core",
                file_path="docs/design/x/approach.md",
                start_line=1,
                end_line=5,
                docstring=None,
                decorators=[],
                is_exported=None,
                is_async=False,
                is_generator=False,
                source_code="## Why core\n\nChose core for performance.",
            ),
        ],
        edges=[_governs_edge(decision_qn, "pkg.core")],
        vectors=[vector_row("pkg.core", axis_vec(0))],
    )
    bundle = await build_bundle(state, SLUG, "explain the core routine")
    by_qn = {item.qualified_name: item for item in bundle.items}

    core_item = by_qn["pkg.core"]
    assert len(core_item.governing_decisions) == 1
    d = core_item.governing_decisions[0]
    assert d.qualified_name == decision_qn
    assert d.heading == "Why core"
    assert d.snippet is not None
    assert "Chose core for performance" in d.snippet


async def test_bundle_no_decisions_when_none_govern(state: CodeIndexState) -> None:
    """Symbols without GOVERNS edges have empty governing_decisions."""
    seed_index(
        state.settings,
        SLUG,
        symbols=[make_symbol("pkg.lone", source_code="def lone():\n    return 1")],
        vectors=[vector_row("pkg.lone", axis_vec(0))],
    )
    bundle = await build_bundle(state, SLUG, "explain lone")
    assert len(bundle.items) == 1
    assert bundle.items[0].governing_decisions == []
