"""Boundary/regression guards for Knowledge slice 1 (build 05): AC 2/8/9."""

from __future__ import annotations

import inspect

from agentalloy.code_index.api import search_router
from agentalloy.code_index.ingest import pipeline
from agentalloy.install.subcommands import knowledge
from agentalloy.storage.protocols import CodeGraphStore

# -- AC 2: capture is composed, not re-invented --------------------------------


def test_decision_sources_exclude_ce_conventions() -> None:
    # docs/architecture-decisions/ and CLAUDE.md are NOT decision sources
    # (consolidated on docs/solutions + approach.md per #375).
    assert pipeline._is_decision_source("docs/architecture-decisions/0001-x.md") is False
    assert pipeline._is_decision_source("CLAUDE.md") is False
    assert pipeline._is_decision_source("docs/CLAUDE.md") is False
    joined = " ".join(pipeline._DECISION_SOURCE_GLOBS)
    assert "architecture-decisions" not in joined
    assert "CLAUDE" not in joined


# -- AC 8: no network / no paid-LLM in the decision path -----------------------


def test_index_decisions_takes_no_network_handle() -> None:
    # The phase receives only a graph store + the markdown change sets — no embed
    # client, no HTTP client — so it cannot make a network/paid-LLM call.
    params = set(inspect.signature(pipeline._index_decisions).parameters)
    assert params == {"graph", "changed", "removed", "chunks"}


def test_live_decision_path_imports_no_cloud_provider() -> None:
    # The vendored engine names cloud providers (engine/constants.py) but is not
    # in the live decision index/query/CLI path.
    for mod in (pipeline, search_router, knowledge):
        src = inspect.getsource(mod)
        assert "engine.constants" not in src, mod.__name__
        assert "api.openai.com" not in src, mod.__name__


# -- AC 9: boundaries hold — decisions live only in the code-index store --------


def test_decision_path_has_no_corpus_handle() -> None:
    # _index_decisions cannot install a corpus skill: it holds only a graph
    # store, whose protocol exposes no skill/fragment/corpus write.
    graph_methods = {m for m in dir(CodeGraphStore) if not m.startswith("__")}
    assert not {m for m in graph_methods if any(k in m for k in ("skill", "fragment", "corpus"))}
    # and the phase's only store parameter is the graph
    assert "graph" in inspect.signature(pipeline._index_decisions).parameters
