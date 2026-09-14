"""End-to-end integration test: MCP + proxy + code graph index + interpreter.

Verifies the full pipeline:
1. Code graph index (OverGraph) indexes the repo (tree-sitter + tantivy + dense)
2. MCP server exposes 14 tools
3. Proxy injects phase-aware steering with accurate token tracking
4. Interpreter drives tool calling with real state persistence
"""

import json
import tempfile
from pathlib import Path

from agentalloy.code_index.fts import FtsIndex
from agentalloy.code_index.open import fts_dir, open_codegraph
from agentalloy.code_index.pipeline import ingest_all_repos
from agentalloy.code_index.retrieval.hybrid import CodeSearcher
from agentalloy.executors import execute_tool, set_graph_index, set_store
from agentalloy.state_store import StateStore


class _StubEmbedder:
    """Fixed 768-d embedder — exercises the dense leg without the embed server."""

    model = "stub-768"

    def is_available(self) -> bool:
        return True

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0 if i % 16 == 0 else 0.0 for i in range(768)] for _ in texts]


def test_full_pipeline_graph_index_to_executors() -> None:
    """Graph index → executors: real code search + symbol lookup."""
    with tempfile.TemporaryDirectory() as tmpdir:
        index_dir = Path(tmpdir) / "index"
        db_path = str(Path(tmpdir) / "state.duck")

        # Fixture repo: two functions with distinctive names.
        repo = Path(tmpdir) / "fixture-repo"
        repo.mkdir()
        (repo / "phase.py").write_text(
            "def phase_advance(target):\n"
            "    return target\n"
            "\n"
            "\n"
            "def proxy_rewrite(usage):\n"
            "    return usage\n"
        )

        # Build the shared index (stub embedder → hybrid, no server needed)
        store = open_codegraph(index_dir)
        reports = ingest_all_repos(
            store,
            [(repo.name, repo)],
            embed_client=_StubEmbedder(),
            index_dir=index_dir,
        )
        assert sum(r.symbols for r in reports) > 0
        assert reports[0].embed_available

        # Inject into executors
        state = StateStore(db_path)
        set_store(state)
        fts = FtsIndex(fts_dir(index_dir))
        set_graph_index(store, CodeSearcher(store, fts, _StubEmbedder()))

        try:
            # Test code_search through executor
            search_result = execute_tool(
                "code_search", json.dumps({"query": "phase_advance", "k": 3})
            )
            parsed = json.loads(search_result)
            assert parsed["count"] > 0
            assert any(
                "phase_advance" in r.get("qualified_name", "")
                or "phase_advance" in r.get("content", "")
                for r in parsed["results"]
            )

            # Test symbols through executor
            sym_result = execute_tool("symbols", json.dumps({"fqn": "proxy"}))
            sym_parsed = json.loads(sym_result)
            assert sym_parsed["count"] > 0

            # Test state tools through executor
            add_result = execute_tool(
                "contract_add",
                json.dumps(
                    {
                        "slug": "test-contract",
                        "domain_tags": ["test"],
                        "touches": "integration test",
                    }
                ),
            )
            assert json.loads(add_result)["status"] == "ok"

            # Verify persistence
            contract = state.get_contract("test-contract")
            assert contract is not None
            assert contract["slug"] == "test-contract"
        finally:
            set_graph_index(None, None)
            set_store(None)
            state.close()
            store.close()


def test_mcp_server_tool_listing() -> None:
    """MCP default surface is SLIM: schemas are resent to the main model
    every turn, so only code_search + contract_detail register by default
    (~230 tokens vs ~1.5k for all 13). Full surface is AGENTALLOY_MCP_TOOLS=full
    (import-time, exercised via the stdio bridge, not in-process here)."""
    import asyncio

    from agentalloy.mcp_server import mcp_server

    tools = asyncio.run(mcp_server.list_tools())
    tool_names = {t.name for t in tools}

    assert tool_names == {"code_search", "contract_detail"}, tool_names


def test_phase_aware_steering_activation_and_diffing() -> None:
    """Phase-aware steering: activation → skills → zero injection."""
    from agentalloy.phase_aware_steering import PhaseAwareSteering
    from agentalloy.skill_engine import SkillEngine

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "state.duck")
        store = StateStore(db_path)
        engine = SkillEngine()
        steering = PhaseAwareSteering(store, engine)

        # Turn 1: activation (full persona) — intake, the lifecycle start
        ctx1, type1 = steering.build_context(prompt="build auth system")
        assert type1 == 2  # activation
        assert "Phase: Intake" in ctx1

        # Turn 2: same phase, skills only or zero
        ctx2, type2 = steering.build_context(prompt="search for auth code")
        assert type2 in (0, 1)  # skills or zero

        # Turn 3: same prompt as turn 2 → zero injection (diffing)
        ctx3, type3 = steering.build_context(prompt="search for auth code")
        assert type3 == 0  # no injection

        # Advance phase
        store.record_artifact("spec", "spec-exit", "done")
        store.advance_phase("design")

        # Turn 4: new phase → activation again
        ctx4, type4 = steering.build_context(prompt="design the API")
        assert type4 == 2  # activation
        assert "Phase: Design" in ctx4

        store.close()


def test_proxy_usage_rewriting() -> None:
    """Proxy rewrites usage to include injected tokens."""
    from agentalloy.proxy import _rewrite_usage

    usage = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
    rewritten = _rewrite_usage(usage, injected_tokens=43)

    assert rewritten["prompt_tokens"] == 143
    assert rewritten["total_tokens"] == 193
    assert rewritten["agentalloy_injected_tokens"] == 43


def test_skill_corpus_size() -> None:
    """Skill corpus has 30+ skills across all phases."""
    from agentalloy.skill_engine import SkillEngine

    engine = SkillEngine()
    skills = engine.list_skills()
    assert len(skills) >= 30

    # Verify all phases are covered
    phases = {s.phase for s in skills}
    expected_phases = {"spec", "design", "plan", "build", "qa", "ship"}
    assert expected_phases.issubset(phases)
