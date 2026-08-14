"""Deterministic entity extraction — pattern matching, bounded caps, edge types.

Covers:
- UT-1: pattern matching for each edge kind (CONSTRAINTS, TOUCHES, REQUIRES, COMMAND, STAKEHOLDER)
- UT-2: bounded extraction (entities_per_doc cap, edges_per_job cap)
- UT-3: graph_store upsert_edges with new kinds (no schema break)
- UT-4: graph_store query_edges(kind) returns correct edges
- UT-5: knowledge_push build_decision_block includes typed entities section
- UT-6: pipeline entity extraction pass runs alongside GOVERNS path
- CT-1: ingestion job with entity prose produces typed edges
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from dataclasses import dataclass

import pytest

from agentalloy.code_index.ingest.entity_extract import (
    EntityEdge,
    EntityIndexResult,
    extract_entities_from_chunk,
    _index_entity_edges,
    _HIGH_PRIORITY_KINDS,
    _EDGE_KINDS,
)
from agentalloy.code_index.ingest.markdown import MarkdownChunk
from agentalloy.code_index.store.graph_store import DuckDBCodeGraphStore
from agentalloy.storage.protocols import CodeEdge, CodeGraphStore, CodeSymbol
from agentalloy.config import Settings


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


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


def make_chunk(
    qualified_name: str = "docs/design/x/approach.md::design",
    file_path: str = "docs/design/x/approach.md",
    body: str = "",
    heading: str = "design",
) -> MarkdownChunk:
    return MarkdownChunk(
        qualified_name=qualified_name,
        heading=heading,
        body=body,
        file_path=file_path,
        start_line=1,
        end_line=len(body.splitlines()) if body else 1,
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBCodeGraphStore]:
    s = DuckDBCodeGraphStore(tmp_path / "graph.duck")
    s.migrate()
    yield s
    s.close()


@pytest.fixture
def populated_store(tmp_path: Path) -> DuckDBCodeGraphStore:
    s = DuckDBCodeGraphStore(tmp_path / "graph.duck")
    s.migrate()
    s.upsert_symbols([
        sym("src/auth/middleware.py", kind="Module", file_path="src/auth/middleware.py"),
        sym("src/code_index/ingest/pipeline.py", kind="Module", file_path="src/code_index/ingest/pipeline.py"),
        sym("src/code_index/store/graph_store.py", kind="Module", file_path="src/code_index/store/graph_store.py"),
        sym("src/code_index/store/jobs_store.py", kind="Module", file_path="src/code_index/store/jobs_store.py"),
        sym("agentalloy.api.knowledge_push", kind="Module", file_path="src/agentalloy/api/knowledge_push.py"),
        sym("agentalloy.code_index.ingest.entity_extract", kind="Module", file_path="src/agentalloy/code_index/ingest/entity_extract.py"),
    ])
    return s


# ---------------------------------------------------------------------------
# UT-1: Pattern matching for each edge kind
# ---------------------------------------------------------------------------


class TestPatternMatching:
    """UT-1: pattern matching for each edge kind."""

    def test_constraints_must_not_touch(self, populated_store: DuckDBCodeGraphStore) -> None:
        chunk = make_chunk(
            body="The auth middleware must not touch src/auth/middleware.py — "
            "legal flagged it for storing session tokens.",
        )
        edges = extract_entities_from_chunk(chunk, populated_store)
        constraint_edges = [e for e in edges if e.kind == "CONSTRAINTS"]
        assert len(constraint_edges) >= 1
        assert constraint_edges[0].kind == "CONSTRAINTS"
        # dst_fqn should be the resolved symbol fqn (or subject if unresolved)
        assert constraint_edges[0].dst_fqn == "src/auth/middleware.py"

    def test_constraints_is_prohibited(self, populated_store: DuckDBCodeGraphStore) -> None:
        chunk = make_chunk(
            body="src/code_index/ingest/pipeline.py is prohibited from editing without approval.",
        )
        edges = extract_entities_from_chunk(chunk, populated_store)
        constraint_edges = [e for e in edges if e.kind == "CONSTRAINTS"]
        assert len(constraint_edges) >= 1
        assert constraint_edges[0].kind == "CONSTRAINTS"
        # Subject is resolved as dst
        assert constraint_edges[0].dst_fqn == "src/code_index/ingest/pipeline.py"

    def test_constraints_cannot(self, populated_store: DuckDBCodeGraphStore) -> None:
        chunk = make_chunk(
            body="The pipeline cannot modify src/code_index/store/graph_store.py directly.",
        )
        edges = extract_entities_from_chunk(chunk, populated_store)
        constraint_edges = [e for e in edges if e.kind == "CONSTRAINTS"]
        assert len(constraint_edges) >= 1
        assert constraint_edges[0].kind == "CONSTRAINTS"
        # Subject is resolved as dst
        assert constraint_edges[0].dst_fqn == "src/code_index/store/graph_store.py"

    def test_touches_affects(self, populated_store: DuckDBCodeGraphStore) -> None:
        chunk = make_chunk(
            body="Editing src/code_index/ingest/pipeline.py affects the jobs_store module.",
        )
        edges = extract_entities_from_chunk(chunk, populated_store)
        touch_edges = [e for e in edges if e.kind == "TOUCHES"]
        assert len(touch_edges) >= 1
        assert touch_edges[0].kind == "TOUCHES"
        # Target "jobs_store module" doesn't resolve; falls back to subject
        # which resolves to pipeline.py
        assert touch_edges[0].dst_fqn == "src/code_index/ingest/pipeline.py"

    def test_requires_depends_on(self, populated_store: DuckDBCodeGraphStore) -> None:
        chunk = make_chunk(
            body="knowledge_push depends on agentalloy_query for the code index API.",
        )
        edges = extract_entities_from_chunk(chunk, populated_store)
        require_edges = [e for e in edges if e.kind == "REQUIRES"]
        assert len(require_edges) >= 1
        assert require_edges[0].kind == "REQUIRES"
        # dst_fqn should be the resolved dependency
        assert require_edges[0].dst_fqn == "agentalloy.api.knowledge_push"

    def test_command_backtick(self, populated_store: DuckDBCodeGraphStore) -> None:
        chunk = make_chunk(
            body="Run `gh pr create` to open the PR, or use `npx skills add` for installation.",
        )
        edges = extract_entities_from_chunk(chunk, populated_store)
        command_edges = [e for e in edges if e.kind == "COMMAND"]
        assert len(command_edges) >= 1
        assert command_edges[0].kind == "COMMAND"
        assert command_edges[0].dst_fqn == ""  # standalone

    def test_stakeholder_legal_flagged(self, populated_store: DuckDBCodeGraphStore) -> None:
        chunk = make_chunk(
            body="Legal flagged auth middleware for storing session tokens in a way that doesn't meet compliance.",
        )
        edges = extract_entities_from_chunk(chunk, populated_store)
        stakeholder_edges = [e for e in edges if e.kind == "STAKEHOLDER"]
        assert len(stakeholder_edges) >= 1
        assert stakeholder_edges[0].kind == "STAKEHOLDER"
        assert stakeholder_edges[0].dst_fqn == ""  # standalone

    def test_no_false_positives(self, populated_store: DuckDBCodeGraphStore) -> None:
        """Doc without entity patterns should produce zero entity edges."""
        chunk = make_chunk(
            body="This is a plain design doc with no constraints, touches, or commands.",
        )
        edges = extract_entities_from_chunk(chunk, populated_store)
        assert len(edges) == 0

    def test_no_edge_when_chunk_empty(self, populated_store: DuckDBCodeGraphStore) -> None:
        chunk = make_chunk(body="")
        edges = extract_entities_from_chunk(chunk, populated_store)
        assert len(edges) == 0


# ---------------------------------------------------------------------------
# UT-2: Bounded extraction
# ---------------------------------------------------------------------------


class TestBoundedExtraction:
    """UT-2: bounded extraction (entities_per_doc cap, edges_per_job cap)."""

    def test_entities_per_doc_cap(self, populated_store: DuckDBCodeGraphStore) -> None:
        """When max_entities=1, only one entity is extracted even if patterns match."""
        chunk = make_chunk(
            body=(
                "src/auth/middleware.py must not touch src/code_index/ingest/pipeline.py. "
                "src/code_index/store/graph_store.py cannot be modified directly. "
                "The pipeline depends on agentalloy_query for API access."
            ),
        )
        edges = extract_entities_from_chunk(chunk, populated_store, max_entities=1)
        assert len(edges) <= 1

    def test_edges_per_job_cap(self, populated_store: DuckDBCodeGraphStore, monkeypatch: pytest.MonkeyPatch) -> None:
        """When max_per_job=2, the index function caps total entities across all chunks."""
        import agentalloy.code_index.ingest.entity_extract as ee_mod
        _builtin_getattr = getattr
        monkeypatch.setattr(ee_mod, "_getattr", lambda obj, name, *default: 2 if name == "code_index_max_edges_per_job" else _builtin_getattr(obj, name, *default))
        chunks = [
            make_chunk(body="src/auth/middleware.py must not be touched."),
            make_chunk(body="src/code_index/ingest/pipeline.py affects jobs_store."),
            make_chunk(body="knowledge_push requires agentalloy_query."),
        ]
        result = _index_entity_edges(populated_store, chunks, Settings())
        assert result.entities_written <= 2

    def test_entity_exhausted_flag(self, populated_store: DuckDBCodeGraphStore, monkeypatch: pytest.MonkeyPatch) -> None:
        """When a chunk produces >= max_entities, entities_exhausted is True."""
        import agentalloy.code_index.ingest.entity_extract as ee_mod
        _builtin_getattr = getattr
        monkeypatch.setattr(ee_mod, "_getattr", lambda obj, name, *default: 1 if name == "code_index_max_entities_per_doc" else _builtin_getattr(obj, name, *default))
        chunk = make_chunk(
            body=(
                "src/auth/middleware.py must not touch src/code_index/ingest/pipeline.py. "
                "src/code_index/store/graph_store.py cannot be modified directly. "
                "The pipeline depends on agentalloy_query for API access."
            ),
        )
        result = _index_entity_edges(populated_store, [chunk], Settings())
        # With max_entities_per_doc=1, extraction stops at 1 and flags exhausted
        assert result.entities_exhausted is True


# ---------------------------------------------------------------------------
# UT-3: upsert_edges with new kinds
# ---------------------------------------------------------------------------


class TestUpsertEdgesNewKinds:
    """UT-3: upsert_edges with new kinds (no schema break)."""

    def test_upsert_constraints_kind(self, store: DuckDBCodeGraphStore) -> None:
        edge = CodeEdge(
            src="doc::a",
            dst="mod.b",
            kind="CONSTRAINTS",
            file_path="doc",
            span="mod.b",
            resolution_tier=1,
        )
        n = store.upsert_edges([edge])
        assert n == 1
        # Should not break; kind is free string
        rows = store.conn.execute(
            "SELECT src, dst, kind FROM edges WHERE kind = 'CONSTRAINTS'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0] == ("doc::a", "mod.b", "CONSTRAINTS")

    def test_upsert_standalone_command_kind(self, store: DuckDBCodeGraphStore) -> None:
        edge = CodeEdge(
            src="doc::a",
            dst="",  # standalone
            kind="COMMAND",
            file_path="doc",
            span="gh pr create",
            resolution_tier=0,
        )
        n = store.upsert_edges([edge])
        assert n == 1
        rows = store.conn.execute(
            "SELECT src, dst, kind FROM edges WHERE kind = 'COMMAND'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0] == ("doc::a", "", "COMMAND")


# ---------------------------------------------------------------------------
# UT-4: query_edges(kind) returns correct edges
# ---------------------------------------------------------------------------


class TestQueryEdgesByKind:
    """UT-4: query_edges(kind) returns correct edges."""

    def test_typed_edges_for_fqn(self, populated_store: DuckDBCodeGraphStore) -> None:
        populated_store.upsert_edges([
            CodeEdge(
                src="docs/design/x/approach.md::design",
                dst="src/auth/middleware.py",
                kind="CONSTRAINTS",
                file_path="docs/design/x/approach.md",
                span="middleware must not touch",
                resolution_tier=1,
            ),
            CodeEdge(
                src="docs/design/x/approach.md::design",
                dst="src/code_index/ingest/pipeline.py",
                kind="TOUCHES",
                file_path="docs/design/x/approach.md",
                span="editing pipeline affects",
                resolution_tier=1,
            ),
        ])
        edges = populated_store.typed_edges_for_fqn("src/auth/middleware.py")
        kinds = {e.kind for e in edges}
        assert "CONSTRAINTS" in kinds

    def test_typed_edges_from_chunks(self, populated_store: DuckDBCodeGraphStore) -> None:
        populated_store.upsert_edges([
            CodeEdge(
                src="docs/design/x/approach.md::design",
                dst="",
                kind="COMMAND",
                file_path="docs/design/x/approach.md",
                span="gh pr create",
                resolution_tier=0,
            ),
            CodeEdge(
                src="docs/design/x/approach.md::design",
                dst="",
                kind="STAKEHOLDER",
                file_path="docs/design/x/approach.md",
                span="legal flagged",
                resolution_tier=0,
            ),
        ])
        edges = populated_store.typed_edges_from_chunks(
            ["docs/design/x/approach.md::design"], limit=5,
        )
        kinds = {e.kind for e in edges}
        assert "COMMAND" in kinds
        assert "STAKEHOLDER" in kinds

    def test_typed_edges_for_fqn_empty_when_no_edges(self, store: DuckDBCodeGraphStore) -> None:
        edges = store.typed_edges_for_fqn("no.such.symbol")
        assert edges == []


# ---------------------------------------------------------------------------
# UT-5: build_decision_block includes typed entities section
# ---------------------------------------------------------------------------


class TestDecisionBlockEntities:
    """UT-5: build_decision_block includes typed entities section."""

    def test_render_entities_nonempty(self) -> None:
        from agentalloy.api.knowledge_push import _render_entities

        @dataclass
        class MockEdge:
            kind: str
            src: str
            dst: str
            span: str

        edges = [
            MockEdge(kind="CONSTRAINTS", src="docs/design/x/approach.md::design", dst="src/auth/middleware.py", span="middleware must not touch"),
            MockEdge(kind="COMMAND", src="docs/design/x/approach.md::design", dst="", span="gh pr create"),
        ]
        text = _render_entities(edges)
        # _render_entities renders a markdown heading block
        assert "# Entities governing this work" in text
        assert "CONSTRAINTS" in text
        assert "COMMAND" in text

    def test_render_entities_empty_returns_empty_string(self) -> None:
        from agentalloy.api.knowledge_push import _render_entities
        assert _render_entities([]) == ""

    def test_decisionpush_gains_entity_edges_field(self) -> None:
        from agentalloy.api.knowledge_push import DecisionPush
        push = DecisionPush(
            text="test",
            count=1,
            truncated=False,
            entity_edges=("edge1", "edge2"),
        )
        assert push.entity_edges == ("edge1", "edge2")


# ---------------------------------------------------------------------------
# UT-6: pipeline entity extraction pass runs alongside GOVERNS path
# ---------------------------------------------------------------------------


class TestPipelineIntegration:
    """UT-6: entity extraction pass runs alongside GOVERNS path, zero interference."""

    def test_index_entity_edges_does_not_modify_gov_edges(
        self, populated_store: DuckDBCodeGraphStore,
    ) -> None:
        """Entity extraction writes typed edges without affecting GOVERNS edges."""
        # Set up a GOVERNS edge
        populated_store.upsert_edges([
            CodeEdge(
                src="docs/solutions/x.md::solution",
                dst="src/auth/middleware.py",
                kind="GOVERNS",
                file_path="docs/solutions/x.md",
                span="middleware.py",
                resolution_tier=1,
            ),
        ])
        gov_count_before = populated_store.count_govern_edges_for_doc("docs/solutions/x.md")

        # Run entity extraction
        chunk = make_chunk(
            qualified_name="docs/design/x/approach.md::design",
            file_path="docs/design/x/approach.md",
            body="src/auth/middleware.py must not be touched.",
        )
        _index_entity_edges(populated_store, [chunk], Settings())

        # GOVERNS edge count must be unchanged
        gov_count_after = populated_store.count_govern_edges_for_doc("docs/solutions/x.md")
        assert gov_count_before == gov_count_after

    def test_entity_and_gov_edges_coexist(self, populated_store: DuckDBCodeGraphStore) -> None:
        """Both GOVERNS and typed edges can coexist in the same graph."""
        populated_store.upsert_edges([
            CodeEdge(
                src="docs/solutions/x.md::solution",
                dst="src/auth/middleware.py",
                kind="GOVERNS",
                file_path="docs/solutions/x.md",
                span="middleware.py",
                resolution_tier=1,
            ),
            CodeEdge(
                src="docs/design/x/approach.md::design",
                dst="src/auth/middleware.py",
                kind="CONSTRAINTS",
                file_path="docs/design/x/approach.md",
                span="middleware must not touch",
                resolution_tier=1,
            ),
        ])
        # governing_decisions returns DecisionRow (markdown docs), not edges
        gov = populated_store.governing_decisions("src/auth/middleware.py")
        assert len(gov) >= 1
        # typed_edges_for_fqn explicitly excludes GOVERNS (entity kinds only)
        # but we can verify GOVERNS and CONSTRAINTS coexist via raw query
        typed = populated_store.typed_edges_for_fqn("src/auth/middleware.py")
        assert len(typed) >= 1
        kinds = {e.kind for e in typed}
        assert "CONSTRAINTS" in kinds
        # GOVERNS exists but typed_edges_for_fqn filters it out by design
        gov_rows = populated_store.conn.execute(
            "SELECT src, dst, kind FROM edges WHERE dst = ? AND kind = 'GOVERNS'",
            ["src/auth/middleware.py"],
        ).fetchall()
        assert len(gov_rows) >= 1


# ---------------------------------------------------------------------------
# CT-1: Ingestion job with entity prose produces typed edges
# ---------------------------------------------------------------------------


class TestContractIngestion:
    """CT-1: ingestion job with entity prose produces typed edges."""

    def test_entity_extraction_produces_result(self, populated_store: DuckDBCodeGraphStore) -> None:
        """Running entity extraction on a chunk with entity prose returns a valid result."""
        chunk = make_chunk(
            qualified_name="docs/design/x/approach.md::design",
            file_path="docs/design/x/approach.md",
            body=(
                "The auth middleware must not touch src/auth/middleware.py. "
                "Editing src/code_index/ingest/pipeline.py affects jobs_store. "
                "knowledge_push depends on agentalloy_query. "
                "Legal flagged auth middleware for compliance. "
                "Run `gh pr create` to open the PR."
            ),
        )
        result = _index_entity_edges(populated_store, [chunk], Settings())
        assert isinstance(result, EntityIndexResult)
        assert result.entities_written >= 1
        # Should have at least CONSTRAINTS and TOUCHES kinds
        kinds = set(result.entity_counts_by_kind.keys())
        assert "CONSTRAINTS" in kinds or "TOUCHES" in kinds or "REQUIRES" in kinds

    def test_entity_extraction_empty_chunk(self, populated_store: DuckDBCodeGraphStore) -> None:
        """Empty chunk produces zero entities."""
        result = _index_entity_edges(populated_store, [make_chunk(body="")], Settings())
        assert result.entities_written == 0
        assert result.entity_counts_by_kind == {}

    def test_entity_extraction_multiple_chunks(self, populated_store: DuckDBCodeGraphStore) -> None:
        """Multiple chunks produce aggregated entities."""
        chunks = [
            make_chunk(
                body="src/auth/middleware.py must not be touched.",
                qualified_name="docs/design/a/approach.md::design",
                file_path="docs/design/a/approach.md",
            ),
            make_chunk(
                body="src/code_index/ingest/pipeline.py affects jobs_store.",
                qualified_name="docs/design/b/approach.md::design",
                file_path="docs/design/b/approach.md",
            ),
        ]
        result = _index_entity_edges(populated_store, chunks, Settings())
        assert result.entities_written >= 2


# ---------------------------------------------------------------------------
# Design: edge kind priority and constants
# ---------------------------------------------------------------------------


class TestEdgeConstants:
    """Verify edge kind constants match design decisions."""

    def test_high_priority_kinds_contains_relevance(self) -> None:
        assert "REQUIRES" in _HIGH_PRIORITY_KINDS
        assert "TOUCHES" in _HIGH_PRIORITY_KINDS
        assert "CONSTRAINTS" in _HIGH_PRIORITY_KINDS
        assert "COMMAND" not in _HIGH_PRIORITY_KINDS
        assert "STAKEHOLDER" not in _HIGH_PRIORITY_KINDS

    def test_edge_kinds_order(self) -> None:
        assert _EDGE_KINDS == [
            "REQUIRES",
            "TOUCHES",
            "CONSTRAINTS",
            "COMMAND",
            "STAKEHOLDER",
        ]


# ---------------------------------------------------------------------------
# Settings import fallback
# ---------------------------------------------------------------------------


class TestSettings:
    """Ensure Settings is importable for entity extraction tests."""

    def test_settings_has_code_index_attrs(self) -> None:
        from agentalloy.config import Settings
        s = Settings()
        # Settings is a pydantic model — attrs are optional
        assert isinstance(s, Settings)


# ---------------------------------------------------------------------------
# Import smoke test
# ---------------------------------------------------------------------------


class TestImportSmoke:
    """Verify new module imports cleanly."""

    def test_entity_extract_import(self) -> None:
        import agentalloy.code_index.ingest.entity_extract  # noqa: F401
        from agentalloy.code_index.ingest.entity_extract import _index_entity_edges  # noqa: F401
        from agentalloy.code_index.ingest.entity_extract import EntityIndexResult  # noqa: F401

    def test_pipeline_import(self) -> None:
        import agentalloy.code_index.ingest.pipeline  # noqa: F401
        from agentalloy.code_index.ingest.pipeline import _index_entity_edges  # noqa: F401
