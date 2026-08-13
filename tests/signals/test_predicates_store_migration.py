"""Tests for slice 06-predicate-and-gate-migration (store-backed predicates).

Covers TB1 (store-only evaluators), TB3 (legacy glob deprecation trace),
and TB6 (3-tag build contract rejection).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from agentalloy.signals.predicates import (
    PredicateContext,
    PredicateResult,
    _derive_phase_from_glob,
    _item_build_contracts,
    eval_build_contract_tag_focus,
    eval_build_contracts_cover_tasks,
    eval_contract_exists,
    eval_contract_has_tags,
)
from agentalloy.storage.state_store import DuckDBStateStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> DuckDBStateStore:
    """Create an empty, in-process DuckDB store with schema initialized."""
    db = tmp_path / "test_state.db"
    store = DuckDBStateStore(db)
    store.open()
    store.migrate()
    return store


@pytest.fixture()
def store_with_contracts(store: DuckDBStateStore) -> DuckDBStateStore:
    """Populate the store with test contracts for TB1.

    The design contract slug MUST match the build contracts' work_item so that
    _resolve_workitem_slug -> _item_build_contracts chain works correctly.
    """
    repo = store._repo()  # type: ignore[attr-defined]
    store.execute(
        f"""
        INSERT INTO sdd_contract (repo, contract_id, slug, domain_tags, work_item, phase, status, updated_at)
        VALUES
            ('{repo}', 'c-001', '01-api-layer', '["api","fastapi"]', '01-widget-design', 'build', 'active', CURRENT_TIMESTAMP),
            ('{repo}', 'c-002', '02-frontend', '["frontend","react"]', '01-widget-design', 'build', 'active', CURRENT_TIMESTAMP),
            ('{repo}', 'c-003', '01-widget-design', '["spec"]', NULL, 'spec', 'active', CURRENT_TIMESTAMP);
        """
    )
    return store


@pytest.fixture()
def ctx(store: DuckDBStateStore, tmp_path: Path) -> PredicateContext:
    """Build a PredicateContext with the store and an empty filesystem."""
    return PredicateContext(
        project_root=tmp_path,
        current_phase="build",
        store=store,
    )


@pytest.fixture()
def ctx_with_contracts(store_with_contracts: DuckDBStateStore, tmp_path: Path) -> PredicateContext:
    """PredicateContext with contracts in the store but empty filesystem."""
    return PredicateContext(
        project_root=tmp_path,
        current_phase="build",
        store=store_with_contracts,
    )


# ---------------------------------------------------------------------------
# TB1 — All four evaluators pass with store rows and empty filesystem
# ---------------------------------------------------------------------------


class TestTB1StoreOnlyEvaluators:
    """All four signal evaluators work with store rows and an empty filesystem."""

    def test_eval_contract_exists_via_store(self, ctx_with_contracts: PredicateContext) -> None:
        """eval_contract_exists queries the store by phase and returns MET when contracts exist."""
        result = eval_contract_exists({"phase": "build"}, ctx_with_contracts)
        assert result == PredicateResult.MET

    def test_eval_contract_exists_missing_via_store(
        self, ctx_with_contracts: PredicateContext
    ) -> None:
        """eval_contract_exists returns NOT_MET for a phase with no contracts."""
        result = eval_contract_exists({"phase": "qa"}, ctx_with_contracts)
        assert result == PredicateResult.NOT_MET

    def test_eval_contract_has_tags_via_store(self, ctx_with_contracts: PredicateContext) -> None:
        """eval_contract_has_tags reads domain_tags from store dicts (any_of list)."""
        result = eval_contract_has_tags(
            {"phase": "build", "any_of": ["api"]},
            ctx_with_contracts,
        )
        assert result == PredicateResult.MET

    def test_eval_contract_has_tags_missing_via_store(
        self, ctx_with_contracts: PredicateContext
    ) -> None:
        """eval_contract_has_tags returns NOT_MET when no contract has the specified tags."""
        result = eval_contract_has_tags(
            {"phase": "build", "any_of": ["nonexistent"]},
            ctx_with_contracts,
        )
        assert result == PredicateResult.NOT_MET

    def test_eval_build_contracts_cover_tasks_via_store(
        self, tmp_path: Path, store_with_contracts: DuckDBStateStore
    ) -> None:
        """eval_build_contracts_cover_tasks queries store for contracts and tasks.md from filesystem."""
        # Cursor points to the design contract slug (sole contract in spec phase)
        cursor = tmp_path / ".agentalloy" / "cursor"
        cursor.parent.mkdir(parents=True, exist_ok=True)
        cursor.write_text("01-widget-design")

        # Create tasks.md for the design work-item
        tasks = tmp_path / "docs" / "design" / "01-widget-design" / "tasks.md"
        tasks.parent.mkdir(parents=True, exist_ok=True)
        tasks.write_text("## Tasks\n- Task 1\n- Task 2\n")

        ctx = PredicateContext(
            project_root=tmp_path,
            current_phase="build",
            store=store_with_contracts,
        )

        result = eval_build_contracts_cover_tasks(
            {"phase": "spec", "tasks": "docs/design/{slug}/tasks.md"},
            ctx,
        )
        # _resolve_workitem_slug returns '01-widget-design' (the spec contract slug)
        # _item_build_contracts filters build contracts by work_item='01-widget-design'
        # 2 build contracts match, 2 tasks → covered
        assert result == PredicateResult.MET

    def test_eval_build_contract_tag_focus_via_store(
        self, tmp_path: Path, store_with_contracts: DuckDBStateStore
    ) -> None:
        """eval_build_contract_tag_focus reads domain_tags from store dicts."""
        # Cursor points to the sole spec contract
        cursor = tmp_path / ".agentalloy" / "cursor"
        cursor.parent.mkdir(parents=True, exist_ok=True)
        cursor.write_text("01-widget-design")

        ctx = PredicateContext(
            project_root=tmp_path,
            current_phase="build",
            store=store_with_contracts,
        )

        result = eval_build_contract_tag_focus(
            {"phase": "spec", "max_tags": 2},
            ctx,
        )
        # Both build contracts have exactly 2 tags, which is <= 2
        assert result == PredicateResult.MET


# ---------------------------------------------------------------------------
# TB3 — Legacy glob arg evaluates and emits deprecation trace
# ---------------------------------------------------------------------------


class TestTB3LegacyGlobTolerance:
    """Legacy `contracts` glob arg still evaluates and emits a deprecation trace."""

    def test_derive_phase_from_glob(self) -> None:
        """_derive_phase_from_glob extracts phase from glob pattern."""
        assert _derive_phase_from_glob(".agentalloy/contracts/active/build/*.md") == "build"
        assert _derive_phase_from_glob(".agentalloy/contracts/active/spec/*.md") == "spec"
        assert _derive_phase_from_glob(".agentalloy/contracts/active/sdd-fast/*.md") == "sdd-fast"

    def test_legacy_glob_arg_contract_exists(
        self, ctx_with_contracts: PredicateContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """eval_contract_exists with legacy `contracts` arg evaluates and traces deprecation."""
        from agentalloy.signals import predicates as pred_mod

        trace_calls: list[tuple[str, ...]] = []

        def mock_trace(glob_pattern: str) -> None:
            trace_calls.append((glob_pattern,))

        monkeypatch.setattr(pred_mod, "_emit_legacy_glob_trace", mock_trace)
        result = eval_contract_exists(
            {
                "contracts": ".agentalloy/contracts/active/build/*.md",
            },
            ctx_with_contracts,
        )

        # Should still evaluate correctly (derives phase='build' from glob)
        assert result == PredicateResult.MET
        assert trace_calls, "Expected _emit_legacy_glob_trace to be called"
        assert trace_calls[0][0] == ".agentalloy/contracts/active/build/*.md"

    def test_legacy_glob_arg_has_tags(
        self, ctx_with_contracts: PredicateContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """eval_contract_has_tags with legacy `contracts` arg evaluates and traces."""
        from agentalloy.signals import predicates as pred_mod

        trace_calls: list[tuple[str, ...]] = []

        def mock_trace(glob_pattern: str) -> None:
            trace_calls.append((glob_pattern,))

        monkeypatch.setattr(pred_mod, "_emit_legacy_glob_trace", mock_trace)
        result = eval_contract_has_tags(
            {
                "contracts": ".agentalloy/contracts/active/build/*.md",
                "any_of": ["api"],
            },
            ctx_with_contracts,
        )

        assert result == PredicateResult.MET
        assert trace_calls, "Expected _emit_legacy_glob_trace to be called"

    def test_legacy_glob_trace_includes_stack(self) -> None:
        """Deprecation trace includes a stack trace for debugging."""
        from agentalloy.signals import predicates as pred_mod

        logger = logging.getLogger("agentalloy.signals.predicates")
        # Temporarily replace the logger's warning to capture output
        orig_warning = logger.warning
        captured: list[str] = []
        logger.warning = lambda *args, **kwargs: captured.append(args[0] % args[1:])  # type: ignore[assignment]
        try:
            pred_mod._emit_legacy_glob_trace(".agentalloy/contracts/active/build/*.md")
        finally:
            logger.warning = orig_warning

        assert captured, "Expected warning to be emitted"
        msg = captured[0]
        assert "DEPRECATION" in msg
        assert "File " in msg, f"Deprecation trace should include stack info, got: {msg[:200]}"


# ---------------------------------------------------------------------------
# TB6 — 3-tag build contract still rejected
# ---------------------------------------------------------------------------


class TestTB6ThreeTagRejection:
    """A build contract with 3+ domain tags is rejected by tag-focus gate."""

    def test_three_tag_contract_rejected_from_store(
        self, tmp_path: Path, store: DuckDBStateStore
    ) -> None:
        """3-tag contract read from store is rejected by tag-focus gate."""
        repo = store._repo()  # type: ignore[attr-defined]
        # Design contract slug matches build contracts' work_item
        store.execute(
            f"""
            INSERT INTO sdd_contract (repo, contract_id, slug, domain_tags, work_item, phase, status, updated_at)
            VALUES
                ('{repo}', 'c-spec', '01-widget-design', '["spec"]', NULL, 'spec', 'active', CURRENT_TIMESTAMP),
                ('{repo}', 'c-001', '01-overloaded', '["api","react","postgres"]', '01-widget-design', 'build', 'active', CURRENT_TIMESTAMP);
            """
        )

        cursor = tmp_path / ".agentalloy" / "cursor"
        cursor.parent.mkdir(parents=True, exist_ok=True)
        cursor.write_text("01-widget-design")

        ctx = PredicateContext(
            project_root=tmp_path,
            current_phase="build",
            store=store,
        )

        result = eval_build_contract_tag_focus(
            {"phase": "spec", "max_tags": 2},
            ctx,
        )
        assert result == PredicateResult.NOT_MET

    def test_three_tag_contract_custom_max(self, tmp_path: Path, store: DuckDBStateStore) -> None:
        """3-tag contract passes when max_tags is raised to 3."""
        repo = store._repo()  # type: ignore[attr-defined]
        store.execute(
            f"""
            INSERT INTO sdd_contract (repo, contract_id, slug, domain_tags, work_item, phase, status, updated_at)
            VALUES
                ('{repo}', 'c-spec', '01-widget-design', '["spec"]', NULL, 'spec', 'active', CURRENT_TIMESTAMP),
                ('{repo}', 'c-001', '01-overloaded', '["api","react","postgres"]', '01-widget-design', 'build', 'active', CURRENT_TIMESTAMP);
            """
        )

        cursor = tmp_path / ".agentalloy" / "cursor"
        cursor.parent.mkdir(parents=True, exist_ok=True)
        cursor.write_text("01-widget-design")

        ctx = PredicateContext(
            project_root=tmp_path,
            current_phase="build",
            store=store,
        )

        result = eval_build_contract_tag_focus(
            {"phase": "spec", "max_tags": 3},
            ctx,
        )
        assert result == PredicateResult.MET


# ---------------------------------------------------------------------------
# _item_build_contracts returns dicts, not Paths
# ---------------------------------------------------------------------------


class TestItemBuildContractsReturnsDicts:
    """_item_build_contracts returns list[dict] from store, not list[Path]."""

    def test_returns_dicts_with_expected_keys(
        self, tmp_path: Path, store_with_contracts: DuckDBStateStore
    ) -> None:
        """_item_build_contracts returns dicts with slug, domain_tags, work_item keys."""
        ctx = PredicateContext(
            project_root=tmp_path,
            current_phase="build",
            store=store_with_contracts,
        )

        contracts = _item_build_contracts(ctx, "01-widget-design")
        assert isinstance(contracts, list)
        assert len(contracts) == 2  # 01-api-layer and 02-frontend

        for c in contracts:
            assert isinstance(c, dict)
            assert "slug" in c
            assert "domain_tags" in c
            assert "work_item" in c

    def test_attribution_by_work_item(self, tmp_path: Path, store: DuckDBStateStore) -> None:
        """Contracts are attributed by work_item field, not filename."""
        repo = store._repo()  # type: ignore[attr-defined]
        store.execute(
            f"""
            INSERT INTO sdd_contract (repo, contract_id, slug, domain_tags, work_item, phase, status, updated_at)
            VALUES
                ('{repo}', 'c-001', '01-foo', '["api"]', 'project-a', 'build', 'active', CURRENT_TIMESTAMP),
                ('{repo}', 'c-002', '01-bar', '["frontend"]', 'project-b', 'build', 'active', CURRENT_TIMESTAMP);
            """
        )

        ctx = PredicateContext(
            project_root=tmp_path,
            current_phase="build",
            store=store,
        )

        contracts = _item_build_contracts(ctx, "project-a")
        assert len(contracts) == 1
        assert contracts[0]["slug"] == "01-foo"

    def test_fallback_when_no_work_item(self, tmp_path: Path, store: DuckDBStateStore) -> None:
        """When no contracts have work_item, falls back to all build contracts."""
        repo = store._repo()  # type: ignore[attr-defined]
        store.execute(
            f"""
            INSERT INTO sdd_contract (repo, contract_id, slug, domain_tags, work_item, phase, status, updated_at)
            VALUES
                ('{repo}', 'c-001', '01-foo', '["api"]', NULL, 'build', 'active', CURRENT_TIMESTAMP),
                ('{repo}', 'c-002', '01-bar', '["frontend"]', NULL, 'build', 'active', CURRENT_TIMESTAMP);
            """
        )

        ctx = PredicateContext(
            project_root=tmp_path,
            current_phase="build",
            store=store,
        )

        # When no work_item is set, returns all build contracts
        contracts = _item_build_contracts(ctx, "any-slug")
        assert len(contracts) == 2
