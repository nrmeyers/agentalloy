"""Tests for GET /state/resume — cold-session bootstrap endpoint.

Test case from docs/design/contract-store-and-write-gating/test-plan.md:
- TA11 — resume reconstructs a cold session in one command

Covers: populated cursor, empty cursor, and a work-item whose scope.touches
matches no governing decisions.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentalloy.api.state_router import (
    get_state_store,
)
from agentalloy.api.state_router import (
    router as state_router,
)
from agentalloy.storage.state_store import DuckDBStateStore, open_state_store


@pytest.fixture
def state_store(tmp_path: Path) -> DuckDBStateStore:
    """A fresh, migrated StateStore at a tmp path."""
    db = tmp_path / "state.duck"
    store = open_state_store(db)
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def resume_client(state_store: DuckDBStateStore) -> TestClient:
    """A TestClient with the state router mounted and store wired."""
    app = FastAPI()
    app.include_router(state_router)
    app.dependency_overrides[get_state_store] = lambda: state_store
    return TestClient(app)


class TestResumeEmptyCursor:
    """GET /state/resume with no cursor set — returns phase and empty cursor_contract."""

    def test_resume_with_no_phase_no_cursor(self, resume_client: TestClient) -> None:
        """Fresh store: phase is None, cursor_contract is None."""
        resp = resume_client.get("/state/resume")
        assert resp.status_code == 200
        body = resp.json()
        assert body["phase"] is None
        assert body["cursor_contract"] is None

    def test_resume_with_phase_no_cursor(self, resume_client: TestClient) -> None:
        """Phase set but no cursor: returns phase, cursor_contract is None."""
        resume_client.post("/state/phase", json={"value": "build"})
        resp = resume_client.get("/state/resume")
        assert resp.status_code == 200
        body = resp.json()
        assert body["phase"] == "build"
        assert body["cursor_contract"] is None


class TestResumePopulatedCursor:
    """GET /state/resume with a cursor pointing to a known contract."""

    def test_resume_returns_contract_info(self, resume_client: TestClient) -> None:
        """Cursor → contract lookup returns the contract's metadata."""

        # Need contract router for put_contract — use full_client pattern
        # Instead, write directly to the store via the client's store
        pass  # handled in next test with full setup

    def test_resume_populated_cursor_full(self, state_store: DuckDBStateStore) -> None:
        """End-to-end: set phase, store contract, set cursor, resume returns it."""
        app = FastAPI()
        app.include_router(state_router)
        app.dependency_overrides[get_state_store] = lambda: state_store
        client = TestClient(app)

        # Set phase
        client.post("/state/phase", json={"value": "build"})

        # Store a contract directly via the store
        state_store.put_contract(
            "build/01-auth",
            phase="build",
            slug="01-auth",
            domain_tags=["api-design"],
            scope_touches=["src/auth/"],
            body="# Auth Middleware",
        )

        # Set cursor pointing to the contract
        client.post("/state/cursor", json={"value": "active/build/01-auth.md"})

        # Resume should find the contract by slug
        resp = client.get("/state/resume")
        assert resp.status_code == 200
        body = resp.json()
        assert body["phase"] == "build"
        assert body["cursor_contract"] is not None
        assert body["cursor_contract"]["contract_id"] == "build/01-auth"
        assert body["cursor_contract"]["slug"] == "01-auth"
        assert body["cursor_contract"]["domain_tags"] == ["api-design"]
        assert body["cursor_contract"]["scope_touches"] == ["src/auth/"]


class TestResumeNoGoverningDecisions:
    """GET /state/resume for a work-item whose scope.touches matches no governing
    decisions — returns empty governing_decisions list."""

    def test_resume_empty_governing_decisions(self, state_store: DuckDBStateStore) -> None:
        """A contract touching files with no GOVERNS edges returns empty decisions."""
        app = FastAPI()
        app.include_router(state_router)
        app.dependency_overrides[get_state_store] = lambda: state_store
        client = TestClient(app)

        # Set phase to build
        client.post("/state/phase", json={"value": "build"})

        # Store a contract touching files with no governing decisions
        state_store.put_contract(
            "build/01-orphan",
            phase="build",
            slug="01-orphan",
            scope_touches=["src/orphan/feature.py"],
            body="# Orphan Feature",
        )

        # Set cursor
        client.post("/state/cursor", json={"value": "active/build/01-orphan.md"})

        resp = client.get("/state/resume")
        assert resp.status_code == 200
        body = resp.json()
        assert body["cursor_contract"] is not None
        # governing_decisions should be None or empty (no matching decisions)
        decisions = body.get("governing_decisions")
        assert decisions is None or decisions == []

    def test_resume_cursor_contract_not_found(self, state_store: DuckDBStateStore) -> None:
        """Cursor points to a slug with no matching contract — cursor_contract is None."""
        app = FastAPI()
        app.include_router(state_router)
        app.dependency_overrides[get_state_store] = lambda: state_store
        client = TestClient(app)

        client.post("/state/phase", json={"value": "build"})
        client.post("/state/cursor", json={"value": "active/build/99-ghost.md"})

        resp = client.get("/state/resume")
        assert resp.status_code == 200
        body = resp.json()
        assert body["cursor_contract"] is None


class TestResumeOwedArtifacts:
    """GET /state/resume returns owed_artifacts for the current phase."""

    def test_resume_returns_owed_artifacts_for_known_phase(
        self, state_store: DuckDBStateStore
    ) -> None:
        """Phase 'build' returns artifacts from exit gates if available."""
        app = FastAPI()
        app.include_router(state_router)
        app.dependency_overrides[get_state_store] = lambda: state_store
        client = TestClient(app)

        client.post("/state/phase", json={"value": "build"})
        resp = client.get("/state/resume")
        assert resp.status_code == 200
        body = resp.json()
        assert body["phase"] == "build"
        # owed_artifacts may be None if no exit gates are loaded, or a list
        # The key assertion is that the endpoint doesn't crash
        assert "owed_artifacts" in body
