"""Regression tests for the proxy gate blindness fix (WI-1: phase-transition-bug).

The proxy evaluated exit gates blind (ctx.store=None, project_root=None), causing:
- Symptom A: intake gate (contract_exists) returned NOT_MET → proxy wrote graph's
  ungated 'ship' as fallback.
- Symptom B: spec/design/plan gates (artifact_exists) returned UNKNOWN → failed open →
  phase advanced one step per turn with no artifact.

The fix: pass the store handle into _build_predicate_context and project_root into
_route_step, so gates evaluate against real store data. These tests verify the
predicate behavior that the fix relies on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentalloy.signals.predicates import (
    PredicateContext,
    PredicateResult,
    eval_artifact_exists,
    eval_contract_exists,
)
from agentalloy.storage.state_store import DuckDBStateStore


@pytest.fixture()
def store(tmp_path: Path) -> DuckDBStateStore:
    """A real store with migrate() called."""
    s = DuckDBStateStore(tmp_path / "test.duck")
    s.open()
    s.migrate()
    return s


class TestContractExistsWithRealStore:
    """eval_contract_exists with a real store evaluates correctly."""

    def test_no_contracts_returns_not_met(self, store: DuckDBStateStore, tmp_path: Path) -> None:
        """With a real store and no contracts, the gate blocks (NOT_MET)."""
        ctx = PredicateContext(project_root=tmp_path, current_phase="intake", store=store)
        result = eval_contract_exists({"phase": "spec"}, ctx)
        assert result == PredicateResult.NOT_MET

    def test_active_contract_returns_met(self, store: DuckDBStateStore, tmp_path: Path) -> None:
        """With a real store and an active contract, the gate passes (MET)."""
        # Insert an active spec contract.
        repo = store._repo()  # type: ignore[attr-defined]  # noqa: SLF001
        store.execute(
            f"INSERT INTO sdd_contract (repo, contract_id, slug, domain_tags, work_item, phase, status, updated_at) "
            f"VALUES ('{repo}', 'spec/widget', 'widget', '[]', NULL, 'spec', 'active', CURRENT_TIMESTAMP)"
        )
        ctx = PredicateContext(project_root=tmp_path, current_phase="intake", store=store)
        result = eval_contract_exists({"phase": "spec"}, ctx)
        assert result == PredicateResult.MET


class TestArtifactExistsWithRealStore:
    """eval_artifact_exists with a real store evaluates correctly."""

    def test_no_artifact_returns_not_met(self, store: DuckDBStateStore, tmp_path: Path) -> None:
        """With a real store and no artifact, the gate blocks (NOT_MET)."""
        ctx = PredicateContext(project_root=tmp_path, current_phase="spec", store=store)
        result = eval_artifact_exists({"phase": "spec", "name": "*.artifact"}, ctx)
        assert result == PredicateResult.NOT_MET

    def test_artifact_present_returns_met(self, store: DuckDBStateStore, tmp_path: Path) -> None:
        """With a real store and an artifact, the gate passes (MET)."""
        # Set a spec artifact.
        store.set_artifact(
            "spec",
            "widget",
            "spec.artifact",
            "# spec\n\n## Acceptance Criteria\n\n- x\n\n## Out of Scope\n\n- y\n",
        )
        ctx = PredicateContext(project_root=tmp_path, current_phase="spec", store=store)
        result = eval_artifact_exists({"phase": "spec", "name": "*.artifact"}, ctx)
        assert result == PredicateResult.MET


class TestStoreNoneBehavior:
    """Verify the store=None behavior that the fix addresses.

    With store=None, the predicates return blind results:
    - eval_contract_exists → NOT_MET (because _query_store_contracts returns [])
    - eval_artifact_exists → UNKNOWN (because _list_store_artifacts returns None)

    The fix passes the store handle so these blind results are replaced with real
    evaluations. These tests document the store=None behavior for regression.
    """

    def test_contract_exists_store_none_returns_not_met(self, tmp_path: Path) -> None:
        """With store=None, eval_contract_exists returns NOT_MET (blind)."""
        ctx = PredicateContext(project_root=tmp_path, current_phase="intake", store=None)
        result = eval_contract_exists({"phase": "spec"}, ctx)
        assert result == PredicateResult.NOT_MET

    def test_artifact_exists_store_none_returns_unknown(self, tmp_path: Path) -> None:
        """With store=None, eval_artifact_exists returns UNKNOWN (fails open)."""
        ctx = PredicateContext(project_root=tmp_path, current_phase="spec", store=None)
        result = eval_artifact_exists({"phase": "spec", "name": "*.artifact"}, ctx)
        assert result == PredicateResult.UNKNOWN
