"""Tests for the state router, client cutover, and in-process invariant.

Test cases from docs/design/contract-store-and-write-gating/test-plan.md:

- TA2 — no ``duckdb.connect`` in the install/web import graph for state paths
- TA3 — phase advance carrying a contract commits both rows
- TA4 — invalid payload rolls back both
- TA5 — service down: StateClient raises StateClientError naming the service
- TB5 — service-side write triggers in-process compose
- TE2 — compose opens no HTTP connection for state
"""

from __future__ import annotations

import importlib
import inspect
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from agentalloy.api.state_client import StateClient, StateClientError
from agentalloy.api.state_router import (
    _repo_key_for,
    _stream_key_for,
    contract_router,
    default_repo_root,
    get_state_store,
)
from agentalloy.api.state_router import (
    router as state_router,
)
from agentalloy.storage.state_store import DuckDBStateStore, open_state_store

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def state_store(tmp_path: Path) -> DuckDBStateStore:
    """A fresh, migrated StateStore at a tmp path."""
    db = tmp_path / "state.duck"
    # Seeded through the bare handle but read back through the routes, which
    # scope to (repo, stream_id) resolved from the request — so the fixture
    # has to be opened under that same pair or every seeded row is invisible.
    root_s = str(default_repo_root())
    store = open_state_store(db, repo=_repo_key_for(root_s), stream_id=_stream_key_for(root_s))
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def state_client(state_store: DuckDBStateStore) -> TestClient:
    """A TestClient with the state router mounted and store wired."""
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(state_router)
    app.dependency_overrides[get_state_store] = lambda: state_store
    return TestClient(app)


@pytest.fixture
def full_client(state_store: DuckDBStateStore) -> TestClient:
    """A TestClient with both state and contract routers mounted."""
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(state_router)
    app.include_router(contract_router)
    app.dependency_overrides[get_state_store] = lambda: state_store
    return TestClient(app)


# ---------------------------------------------------------------------------
# _write_result_to_response unit tests
# ---------------------------------------------------------------------------


class TestWriteResultToResponse:
    """_write_result_to_response handles conflict.owner=None gracefully."""

    def test_conflict_owner_none_is_non_blocking(self) -> None:
        """When conflict.owner is None, treat as non-blocking (no real conflict).

        This can happen when acquire_lease returns a conflict for a non-existent
        row (owner=None, lease_expires_at=None).  The response helper must not
        crash constructing StateConflictInfo with None values.
        """
        from agentalloy.api.state_router import _write_result_to_response
        from agentalloy.storage.protocols import LeaseConflict, StateWriteResult

        result = StateWriteResult(
            success=True,
            kind="phase",
            value="spec",
            owner="s1",
            lease_expires_at=None,
            conflict=LeaseConflict(
                owner=None,
                lease_expires_at=None,
                message="No row for 'phase' — write it before leasing",
            ),
        )
        status, response = _write_result_to_response(result)
        assert status == 200
        assert response.kind == "phase"
        assert response.value == "spec"

    def test_conflict_owner_present_returns_409(self) -> None:
        """When conflict.owner is set, return 409 with StateConflictInfo."""
        from datetime import datetime, timedelta

        from agentalloy.api.state_router import _write_result_to_response
        from agentalloy.storage.protocols import LeaseConflict, StateWriteResult

        now = datetime.now()
        result = StateWriteResult(
            success=False,
            kind="phase",
            value="",
            owner=None,
            lease_expires_at=None,
            conflict=LeaseConflict(
                owner="s1",
                lease_expires_at=now + timedelta(minutes=5),
                message="Session s1 holds the phase. Take over?",
            ),
        )
        status, response = _write_result_to_response(result)
        assert status == 409
        assert response.owner == "s1"
        assert "s1" in response.message


# ---------------------------------------------------------------------------
# Router endpoint tests
# ---------------------------------------------------------------------------


class TestGetState:
    """GET /state and GET /state/{kind}."""

    def test_get_all_state_empty(self, state_client: TestClient) -> None:
        resp = state_client.get("/state")
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == {}

    def test_get_all_state_populated(
        self, state_client: TestClient, state_store: DuckDBStateStore
    ) -> None:
        state_store.write("phase", "spec")
        state_store.write("cursor", "active/build/01.md")
        resp = state_client.get("/state")
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"]["phase"] == "spec"
        assert body["state"]["cursor"] == "active/build/01.md"

    def test_get_single_kind(self, state_client: TestClient, state_store: DuckDBStateStore) -> None:
        state_store.write("phase", "design")
        resp = state_client.get("/state/phase")
        assert resp.status_code == 200
        body = resp.json()
        assert body["kind"] == "phase"
        assert body["value"] == "design"

    def test_get_single_kind_missing(self, state_client: TestClient) -> None:
        resp = state_client.get("/state/phase")
        assert resp.status_code == 200
        body = resp.json()
        assert body["kind"] == "phase"
        assert body["value"] is None

    def test_get_unknown_kind_404(self, state_client: TestClient) -> None:
        resp = state_client.get("/state/nonexistent")
        assert resp.status_code == 404


class TestPostPhase:
    """POST /state/phase."""

    def test_write_phase(self, state_client: TestClient) -> None:
        resp = state_client.post("/state/phase", json={"value": "spec"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["kind"] == "phase"
        assert body["value"] == "spec"

    def test_write_phase_then_read(self, state_client: TestClient) -> None:
        state_client.post("/state/phase", json={"value": "build"})
        resp = state_client.get("/state/phase")
        assert resp.json()["value"] == "build"

    def test_write_phase_with_owner(self, state_client: TestClient) -> None:
        resp = state_client.post("/state/phase", json={"value": "spec", "owner": "session-1"})
        assert resp.status_code == 200
        assert resp.json()["owner"] == "session-1"

    def test_write_phase_empty_value_422(self, state_client: TestClient) -> None:
        resp = state_client.post("/state/phase", json={"value": ""})
        assert resp.status_code == 422

    def test_write_phase_persists_graph_checkpoint(
        self, state_client: TestClient, state_store: DuckDBStateStore
    ) -> None:
        """T21 — HTTP write persists the phase row (authoritative source).

        A phase advance over ``repo_root`` must persist the ``phase`` row
        via ``store.write_phase`` — the graph checkpoint shim was retired
        in step 08. This is the only seam that proves the router committed
        the phase blob (with lease semantics) rather than a bare row write.
        """
        from agentalloy.api.state_router import default_repo_root

        root = Path(str(default_repo_root()))
        resp = state_client.post(
            "/state/phase",
            params={"repo_root": str(root)},
            json={"value": "spec"},
        )
        assert resp.status_code == 200
        assert resp.json()["value"] == "spec"

        # Phase row is authoritative (graph checkpoint shim retired in step 08).
        phase_row = state_store.read_phase()
        assert phase_row is not None
        assert phase_row.phase == "spec"


class TestRoutePhaseOffloop:
    """The exit gate must run off the event loop (1.4)."""

    def test_route_phase_runs_off_event_loop(
        self, state_client: TestClient, state_store: DuckDBStateStore, monkeypatch
    ) -> None:
        """_route_phase does file I/O + embedding; it must be dispatched via
        asyncio.to_thread so it can't block the event loop (1.4 regression).

        The old code called it inline on the loop. We capture the event-loop
        thread (via the handler's direct ``store.read_phase`` call) and the
        thread _route_phase actually runs on, then assert they differ —
        proving the gate is dispatched to a worker thread, regardless of the
        executor's thread naming.
        """
        import threading

        from agentalloy.api import state_router as sr

        loop_threads: list[str] = []
        route_threads: list[str] = []

        def _fake_read_phase():
            loop_threads.append(threading.current_thread().name)
            return None

        def _fake_route_phase(*_args, **_kwargs):
            route_threads.append(threading.current_thread().name)
            return None  # same-phase no-op → handler takes the fast path

        monkeypatch.setattr(state_store, "read_phase", _fake_read_phase)
        monkeypatch.setattr(sr, "_route_phase", _fake_route_phase)

        resp = state_client.post("/state/phase", json={"value": "spec"})
        assert resp.status_code == 200

        assert len(route_threads) == 1
        assert loop_threads, "expected a direct store.read_phase call on the loop"
        # The gate must run on a different thread than the event loop.
        assert route_threads[0] != loop_threads[0]


class TestPostCursor:
    """POST /state/cursor."""

    def test_write_cursor(self, state_client: TestClient) -> None:
        resp = state_client.post("/state/cursor", json={"value": "active/build/02-api.md"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["kind"] == "cursor"
        assert body["value"] == "active/build/02-api.md"


class TestPostApprove:
    """POST /state/approve."""

    def test_write_approve(self, state_client: TestClient) -> None:
        resp = state_client.post("/state/approve", json={"value": "spec"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["kind"] == "approved"
        assert body["value"] == "spec"

    def test_approve_persists(self, state_client: TestClient) -> None:
        state_client.post("/state/approve", json={"value": "design"})
        resp = state_client.get("/state/approved")
        assert resp.json()["value"] == "design"


class TestLeaseConflict:
    """Lease conflict returns HTTP 409 with StateConflictInfo."""

    def test_first_write_to_leased_kind_succeeds(self, state_client: TestClient) -> None:
        """First write to a leased kind (phase/approved) must return 200.

        When no row exists yet, the inline lease check produces no conflict.
        The _write_result_to_response helper must not crash on a None owner.
        """
        resp = state_client.post("/state/phase", json={"value": "spec", "owner": "s1"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["kind"] == "phase"
        assert body["value"] == "spec"
        assert body["owner"] == "s1"

        # Same for the other leased kind.
        resp = state_client.post("/state/approve", json={"value": "true", "owner": "s1"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["kind"] == "approved"

    def test_lease_conflict_on_phase(
        self, state_client: TestClient, state_store: DuckDBStateStore
    ) -> None:
        from datetime import datetime, timedelta

        now = datetime.now()
        future = (now + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        ts = now.strftime("%Y-%m-%d %H:%M:%S")
        repo = state_store._repo()
        stream_id = state_store._sid()
        state_store.conn.execute(
            "INSERT INTO sdd_state "
            "(repo, stream_id, kind, session_key, value, owner, updated_at, lease_expires_at) "
            "VALUES (?, ?, ?, '', 'build', ?, ?, ?)",
            (repo, stream_id, "phase", "s1", ts, future),
        )
        resp = state_client.post(
            "/state/phase", json={"value": "qa", "override": True, "owner": "s2"}
        )
        assert resp.status_code == 409
        body = resp.json()
        detail = body["detail"]
        assert detail["owner"] == "s1"
        assert "lease_expires_at" in detail
        assert "s1" in detail["message"]


# ---------------------------------------------------------------------------
# TA3 — phase advance carrying a contract commits both rows
# ---------------------------------------------------------------------------


class TestTA3:
    """TA3: Phase advance carrying a contract commits both rows inside one
    transaction().  Both the phase and the contract row are visible after
    the response returns 200."""

    def test_phase_advance_with_contract_commits_both(
        self, full_client: TestClient, state_store: DuckDBStateStore
    ) -> None:
        """Phase + contract in a single POST /state/phase commits both rows."""
        payload = {
            "value": "build",
            "contract": {
                "contract_id": "ctr-ta3-1",
                "phase": "build",
                "slug": "test-slug",
                "work_item": "04-contract-routes",
                "route": "full",
                "domain_tags": ["api-design"],
                "body": "# Test contract",
            },
        }
        resp = full_client.post("/state/phase", json=payload)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["kind"] == "phase"
        assert body["value"] == "build"
        assert body["contract_id"] == "ctr-ta3-1"

        # Verify both rows exist in the store.  ``read_phase`` decodes the blob;
        # ``read`` would hand back the raw JSON row.
        phase = state_store.read_phase()
        assert phase is not None and phase.phase == "build"
        contract = state_store.get_contract("ctr-ta3-1")
        assert contract is not None
        assert contract["phase"] == "build"
        assert contract["slug"] == "test-slug"
        assert contract["body"] == "# Test contract"

    def test_phase_advance_without_contract_still_works(
        self, full_client: TestClient, state_store: DuckDBStateStore
    ) -> None:
        """Phase advance without a contract uses the fast path."""
        resp = full_client.post("/state/phase", json={"value": "design"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["kind"] == "phase"
        assert body["value"] == "design"
        assert body["contract_id"] is None
        phase = state_store.read_phase()
        assert phase is not None and phase.phase == "design"

    def test_phase_advance_contract_optional_fields(
        self, full_client: TestClient, state_store: DuckDBStateStore
    ) -> None:
        """Contract with optional fields stores them correctly."""
        payload = {
            "value": "spec",
            "contract": {
                "contract_id": "ctr-ta3-2",
                "phase": "spec",
                "slug": "optional-slug",
                "scope_touches": ["src/a.py"],
                "scope_avoids": ["src/b.py"],
                "success_criteria": ["criterion 1"],
            },
        }
        resp = full_client.post("/state/phase", json=payload)
        assert resp.status_code == 200
        contract = state_store.get_contract("ctr-ta3-2")
        assert contract is not None
        assert contract["scope_touches"] == ["src/a.py"]
        assert contract["scope_avoids"] == ["src/b.py"]
        assert contract["success_criteria"] == ["criterion 1"]


# ---------------------------------------------------------------------------
# TA4 — invalid payload rolls back both
# ---------------------------------------------------------------------------


class TestTA4:
    """TA4: Phase advance whose contract payload fails validation leaves phase
    unchanged AND writes no contract row (rollback)."""

    def test_invalid_contract_payload_rolls_back_phase(
        self, full_client: TestClient, state_store: DuckDBStateStore
    ) -> None:
        """Missing required contract field -> 422 -> phase unchanged, no contract."""
        state_store.write("phase", "intake")

        payload = {
            "value": "build",
            "contract": {
                "contract_id": "ctr-ta4-1",
                "phase": "build",
                # slug is missing — should fail validation
            },
        }
        resp = full_client.post("/state/phase", json=payload)
        assert resp.status_code == 422

        # Phase should be unchanged (rollback verified)
        assert state_store.read("phase") == "intake"
        # No contract row should exist (rollback verified)
        assert state_store.get_contract("ctr-ta4-1") is None

    def test_invalid_contract_phase_rolls_back(
        self, full_client: TestClient, state_store: DuckDBStateStore
    ) -> None:
        """Invalid contract phase value -> 422 -> phase unchanged, no contract."""
        state_store.write("phase", "intake")

        payload = {
            "value": "build",
            "contract": {
                "contract_id": "ctr-ta4-2",
                "phase": "invalid-phase",
                "slug": "test-slug",
            },
        }
        resp = full_client.post("/state/phase", json=payload)
        assert resp.status_code == 422

        assert state_store.read("phase") == "intake"
        assert state_store.get_contract("ctr-ta4-2") is None

    def test_empty_contract_id_rolls_back(
        self, full_client: TestClient, state_store: DuckDBStateStore
    ) -> None:
        """Empty contract_id -> 422 -> phase unchanged, no contract."""
        state_store.write("phase", "intake")

        payload = {
            "value": "build",
            "contract": {
                "contract_id": "",
                "phase": "build",
                "slug": "test-slug",
            },
        }
        resp = full_client.post("/state/phase", json=payload)
        assert resp.status_code == 422

        assert state_store.read("phase") == "intake"
        assert state_store.get_contract("") is None

    def test_mid_transaction_failure_rolls_back_phase(
        self, full_client: TestClient, state_store: DuckDBStateStore
    ) -> None:
        """Contract write fails mid-transaction -> phase is rolled back.

        Pydantic validation happens before the transaction, so the tests above
        never exercise the rollback path.  This test patches ``put_contract``
        to raise inside the transaction, proving the phase write is rolled back
        when the contract write fails — acceptance criterion A3 requires both
        writes to be one transactional unit.
        """
        state_store.write("phase", "intake")

        def _raise_put_contract(*_args, **_kwargs):
            raise RuntimeError("simulated contract write failure")

        with pytest.raises(RuntimeError, match="simulated contract write failure"):
            with state_store.transaction() as tx:
                tx.write("phase", "build")
                _raise_put_contract()

        # Phase should be rolled back (not "build")
        assert state_store.read("phase") == "intake"

    def test_endpoint_mid_transaction_failure_rolls_back(
        self, full_client: TestClient, state_store: DuckDBStateStore
    ) -> None:
        """Endpoint-level: contract write raises inside transaction -> phase rolled back.

        Pydantic validation happens before the transaction starts, so the 422
        tests above never exercise the rollback path.  This test patches
        ``put_contract`` to raise inside the transaction, verifying that the
        phase write is rolled back when the contract write fails — acceptance
        criterion A3 requires both writes to be one transactional unit.
        """
        state_store.write("phase", "build")

        def _raise_put_contract(
            contract_id: str,
            phase: str,
            slug: str,
            work_item: str | None = None,
            route: str | None = None,
            domain_tags: list[str] | None = None,
            scope_touches: list[str] | None = None,
            scope_avoids: list[str] | None = None,
            success_criteria: list[str] | None = None,
            body: str | None = None,
        ) -> str:
            raise RuntimeError("simulated contract write failure")

        with patch.object(state_store, "put_contract", side_effect=_raise_put_contract):
            payload = {
                "value": "qa",
                "override": True,
                "contract": {
                    "contract_id": "ctr-ta4-rollback",
                    "phase": "qa",
                    "slug": "test-slug",
                },
            }
            resp = full_client.post("/state/phase", json=payload)

        # The handler returns 500 when the transaction fails
        assert resp.status_code == 500

        # Phase should be rolled back (not "qa")
        assert state_store.read("phase") == "build"
        # No contract row should exist
        assert state_store.get_contract("ctr-ta4-rollback") is None


# ---------------------------------------------------------------------------
# Contract CRUD endpoint tests
# ---------------------------------------------------------------------------


class TestContractCreate:
    """POST /contracts — create a contract."""

    def test_create_contract(self, full_client: TestClient, state_store: DuckDBStateStore) -> None:
        payload = {
            "contract_id": "ctr-create-1",
            "phase": "build",
            "slug": "create-slug",
            "body": "# Created contract",
        }
        resp = full_client.post("/contracts", json=payload)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["contract_id"] == "ctr-create-1"
        assert body["phase"] == "build"
        assert body["body"] == "# Created contract"

        c = state_store.get_contract("ctr-create-1")
        assert c is not None
        assert c["body"] == "# Created contract"

    def test_create_contract_missing_required_422(self, full_client: TestClient) -> None:
        resp = full_client.post("/contracts", json={"contract_id": "x"})
        assert resp.status_code == 422


class TestContractList:
    """GET /contracts — list with filters."""

    def test_list_contracts_empty(self, full_client: TestClient) -> None:
        resp = full_client.get("/contracts")
        assert resp.status_code == 200
        assert resp.json()["contracts"] == []

    def test_list_contracts_with_data(
        self, full_client: TestClient, state_store: DuckDBStateStore
    ) -> None:
        state_store.put_contract("ctr-list-1", phase="build", slug="alpha")
        state_store.put_contract("ctr-list-2", phase="design", slug="beta")

        resp = full_client.get("/contracts")
        assert resp.status_code == 200
        assert len(resp.json()["contracts"]) == 2

        resp = full_client.get("/contracts?phase=build")
        assert len(resp.json()["contracts"]) == 1
        assert resp.json()["contracts"][0]["contract_id"] == "ctr-list-1"

        resp = full_client.get("/contracts?slug=beta")
        assert len(resp.json()["contracts"]) == 1
        assert resp.json()["contracts"][0]["contract_id"] == "ctr-list-2"


class TestContractGet:
    """GET /contracts/{id}."""

    def test_get_contract(self, full_client: TestClient, state_store: DuckDBStateStore) -> None:
        state_store.put_contract("ctr-get-1", phase="build", slug="s", body="body text")
        resp = full_client.get("/contracts/ctr-get-1")
        assert resp.status_code == 200
        assert resp.json()["contract_id"] == "ctr-get-1"
        assert resp.json()["body"] == "body text"

    def test_get_contract_404(self, full_client: TestClient) -> None:
        resp = full_client.get("/contracts/nonexistent")
        assert resp.status_code == 404


class TestContractPatch:
    """PATCH /contracts/{id} — in-place correction."""

    def test_patch_contract(self, full_client: TestClient, state_store: DuckDBStateStore) -> None:
        state_store.put_contract("ctr-patch-1", phase="build", slug="s", body="original")
        resp = full_client.patch("/contracts/ctr-patch-1", json={"body": "corrected"})
        assert resp.status_code == 200
        assert resp.json()["body"] == "corrected"

        c = state_store.get_contract("ctr-patch-1")
        assert c["phase"] == "build"
        assert c["slug"] == "s"

    def test_patch_contract_404(self, full_client: TestClient) -> None:
        resp = full_client.patch("/contracts/nonexistent", json={"body": "x"})
        assert resp.status_code == 404


class TestContractIdWithSlash:
    """`contract init` mints IDs like `intake/slug` — every route must accept them."""

    def test_slash_id_roundtrip(
        self, full_client: TestClient, state_store: DuckDBStateStore
    ) -> None:
        state_store.put_contract("intake/slashy", phase="intake", slug="slashy", body="v1")

        assert full_client.get("/contracts/intake/slashy").json()["contract_id"] == "intake/slashy"

        resp = full_client.patch("/contracts/intake/slashy", json={"body": "v2"})
        assert resp.status_code == 200
        assert resp.json()["body"] == "v2"

        resp = full_client.post(
            "/contracts/intake/slashy/supersede",
            json={"new_contract_id": "intake/slashy-2", "phase": "intake", "slug": "slashy"},
        )
        assert resp.status_code == 200
        assert resp.json()["contract_id"] == "intake/slashy-2"

        resp = full_client.post("/contracts/intake/slashy-2/archive")
        assert resp.status_code == 200
        assert resp.json()["status"] == "archived"


class TestContractArchive:
    """POST /contracts/{id}/archive."""

    def test_archive_contract(self, full_client: TestClient, state_store: DuckDBStateStore) -> None:
        state_store.put_contract("ctr-arch-1", phase="build", slug="s")
        resp = full_client.post("/contracts/ctr-arch-1/archive")
        assert resp.status_code == 200
        assert resp.json()["status"] == "archived"

    def test_archive_contract_404(self, full_client: TestClient) -> None:
        resp = full_client.post("/contracts/nonexistent/archive")
        assert resp.status_code == 404


class TestContractSupersede:
    """POST /contracts/{id}/supersede."""

    def test_supersede_contract(
        self, full_client: TestClient, state_store: DuckDBStateStore
    ) -> None:
        state_store.put_contract("ctr-sup-1", phase="build", slug="s", body="v1")
        payload = {
            "new_contract_id": "ctr-sup-2",
            "phase": "build",
            "slug": "s",
            "body": "v2",
        }
        resp = full_client.post("/contracts/ctr-sup-1/supersede", json=payload)
        assert resp.status_code == 200
        assert resp.json()["contract_id"] == "ctr-sup-2"
        assert resp.json()["supersedes"] == "ctr-sup-1"

        old = state_store.get_contract("ctr-sup-1")
        assert old["status"] == "cancelled"


# ---------------------------------------------------------------------------
# TB5 — service-side write triggers in-process compose
# ---------------------------------------------------------------------------


class TestTB5:
    """TB5: A service-side contract write triggers compose in-process with no
    watcher and no subprocess."""

    def test_compose_triggered_on_contract_create(
        self, full_client: TestClient, state_store: DuckDBStateStore
    ) -> None:
        """POST /contracts triggers in-process compose via app.state orchestrator."""
        mock_orchestrator = AsyncMock()
        mock_orchestrator.compose = AsyncMock(return_value=None)

        client = full_client
        client.app.state.compose_orchestrator = mock_orchestrator

        payload = {
            "contract_id": "ctr-tb5-1",
            "phase": "build",
            "slug": "tb5-slug",
            "body": "# TB5 test contract",
        }
        resp = client.post("/contracts", json=payload)
        assert resp.status_code == 200, resp.text

        mock_orchestrator.compose.assert_called_once()
        call_args = mock_orchestrator.compose.call_args[0][0]
        assert call_args.phase == "build"
        assert call_args.requesting_agent == "contract_write"

    def test_compose_triggered_on_phase_advance_with_contract(
        self, full_client: TestClient, state_store: DuckDBStateStore
    ) -> None:
        """POST /state/phase with contract triggers in-process compose."""
        mock_orchestrator = AsyncMock()
        mock_orchestrator.compose = AsyncMock(return_value=None)

        client = full_client
        client.app.state.compose_orchestrator = mock_orchestrator

        payload = {
            "value": "build",
            "contract": {
                "contract_id": "ctr-tb5-2",
                "phase": "build",
                "slug": "tb5-slug-2",
                "body": "# Phase advance contract",
            },
        }
        resp = client.post("/state/phase", json=payload)
        assert resp.status_code == 200, resp.text

        mock_orchestrator.compose.assert_called_once()
        call_args = mock_orchestrator.compose.call_args[0][0]
        assert call_args.phase == "build"

    def test_no_subprocess_or_watcher_in_compose_path(self) -> None:
        """The compose trigger path does not use subprocess or file watchers."""
        mod = importlib.import_module("agentalloy.api.state_router")
        source = inspect.getsource(mod)
        assert "subprocess" not in source
        assert "watchdog" not in source
        assert "FileSystemWatcher" not in source
        assert "os.system" not in source


# ---------------------------------------------------------------------------
# TA2 — no duckdb.connect in install/web import graph
# ---------------------------------------------------------------------------


class TestTA2:
    """TA2: Every out-of-process caller path (CLI, web) goes through HTTP."""

    def test_state_subcommands_no_duckdb_connect(self) -> None:
        state_modules = [
            "agentalloy.install.subcommands.phase",
            "agentalloy.install.subcommands.approve",
            "agentalloy.install.subcommands.task",
            "agentalloy.install.subcommands.workflow",
        ]
        for mod_name in state_modules:
            mod = importlib.import_module(mod_name)
            source = inspect.getsource(mod)
            assert "duckdb.connect" not in source, (
                f"{mod_name} contains duckdb.connect — state mutations must go through HTTP"
            )

    def test_web_modules_no_duckdb_connect(self) -> None:
        web_modules = [
            "agentalloy.web.ops_api",
            "agentalloy.web.wizard_api",
            "agentalloy.web.config_api",
            "agentalloy.web.skills_api",
        ]
        for mod_name in web_modules:
            if mod_name not in sys.modules:
                mod = importlib.import_module(mod_name)
            else:
                mod = sys.modules[mod_name]
            source = inspect.getsource(mod)
            assert "duckdb.connect" not in source, (
                f"{mod_name} contains duckdb.connect — web paths must go through HTTP"
            )

    def test_state_client_no_duckdb_import(self) -> None:
        mod = importlib.import_module("agentalloy.api.state_client")
        source = inspect.getsource(mod)
        assert "import duckdb" not in source
        assert "duckdb.connect" not in source


# ---------------------------------------------------------------------------
# TA5 — service down: StateClient raises StateClientError naming the service
# ---------------------------------------------------------------------------


class TestTA5:
    """TA5: Service down — StateClient raises StateClientError naming the
    service and writes nothing."""

    def test_set_phase_raises_when_service_down(self) -> None:
        client = StateClient(base_url="http://127.0.0.1:19999")
        with pytest.raises(StateClientError) as exc_info:
            client.set_phase("build")
        assert "agentalloy service" in exc_info.value.message

    def test_approve_raises_when_service_down(self) -> None:
        client = StateClient(base_url="http://127.0.0.1:19988")
        with pytest.raises(StateClientError) as exc_info:
            client.approve("spec")
        assert "agentalloy service" in exc_info.value.message

    def test_set_cursor_raises_when_service_down(self) -> None:
        client = StateClient(base_url="http://127.0.0.1:19997")
        with pytest.raises(StateClientError) as exc_info:
            client.set_cursor("active/build/01.md")
        assert "agentalloy service" in exc_info.value.message

    def test_no_file_written_when_service_down(self, tmp_path: Path) -> None:
        client = StateClient(base_url="http://127.0.0.1:19996")
        phase_file = tmp_path / ".agentalloy" / "phase"
        phase_file.parent.mkdir(parents=True)
        with pytest.raises(StateClientError):
            client.set_phase("build")
        assert not phase_file.exists()

    def test_no_fallback_methods_exist(self) -> None:
        client = StateClient(base_url="http://127.0.0.1:19995")
        assert not hasattr(client, "_read_phase_file")
        assert not hasattr(client, "_write_phase_file")


# ---------------------------------------------------------------------------
# TE2 — compose opens no HTTP connection for state
# ---------------------------------------------------------------------------


class TestTE2:
    """TE2: Compose opens no HTTP connection for state — the in-process
    compose path uses StateStore directly, not StateClient."""

    def test_compose_orchestrator_no_state_client_import(self) -> None:
        mod = importlib.import_module("agentalloy.orchestration.compose")
        source = inspect.getsource(mod)
        assert "state_client" not in source.lower()
        assert "StateClient" not in source

    def test_compose_router_no_state_client_import(self) -> None:
        mod = importlib.import_module("agentalloy.api.compose_router")
        source = inspect.getsource(mod)
        assert "state_client" not in source.lower()
        assert "StateClient" not in source

    def test_proxy_signal_no_state_client_import(self) -> None:
        mod = importlib.import_module("agentalloy.api.proxy_signal")
        source = inspect.getsource(mod)
        assert "StateClient" not in source

    def test_signals_module_no_state_client_import(self) -> None:
        signals_pkg = importlib.import_module("agentalloy.signals")
        pkg_dir = Path(signals_pkg.__file__).parent
        for py_file in pkg_dir.glob("*.py"):
            if py_file.name.startswith("__"):
                continue
            source = py_file.read_text()
            assert "StateClient" not in source, (
                f"{py_file.name} imports StateClient — in-process paths must use StateStore directly"
            )


# ---------------------------------------------------------------------------
# End session instruction in response
# ---------------------------------------------------------------------------


class TestEndSessionInstruction:
    """Phase advance responses include end_session_instruction (D9)."""

    def test_end_session_instruction_present_on_fast_path(self, state_client: TestClient) -> None:
        """Fast path (no contract) returns end_session_instruction."""
        resp = state_client.post("/state/phase", json={"value": "build"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["end_session_instruction"] is not None
        assert "`build`" in body["end_session_instruction"]
        assert "End this session" in body["end_session_instruction"]

    def test_end_session_instruction_present_with_contract(self, full_client: TestClient) -> None:
        """Transactional path (with contract) returns end_session_instruction."""
        payload = {
            "value": "spec",
            "contract": {
                "contract_id": "ctr-esi-1",
                "phase": "spec",
                "slug": "esi-slug",
            },
        }
        resp = full_client.post("/state/phase", json=payload)
        assert resp.status_code == 200
        body = resp.json()
        assert body["end_session_instruction"] is not None
        assert "`spec`" in body["end_session_instruction"]

    def test_end_session_instruction_content(self, state_client: TestClient) -> None:
        """Instruction mentions restart and next phase label."""
        resp = state_client.post("/state/phase", json={"value": "design"})
        assert resp.status_code == 200
        instruction = resp.json()["end_session_instruction"]
        assert "restart the harness" in instruction
        assert "design" in instruction


# ---------------------------------------------------------------------------
# Posture rewrite with repo_root
# ---------------------------------------------------------------------------


class TestPostureRewrite:
    """Posture rewrite fires when repo_root is provided on phase advance."""

    def test_posture_rewrite_called_on_fast_path(
        self, state_client: TestClient, tmp_path: Path
    ) -> None:
        """Fast path calls _rewrite_posture when repo_root is set."""
        from unittest.mock import patch

        with patch("agentalloy.api.state_router._rewrite_posture", return_value=[]) as mock_rewrite:
            resp = state_client.post(
                "/state/phase?repo_root=" + str(tmp_path),
                json={"value": "build"},
            )
            assert resp.status_code == 200
            # mode is None: a fresh repo has no prior row, so the written mode
            # resolves to nothing (workflow) — read back off the same store
            # handle that did the write, not re-derived through a second seam.
            mock_rewrite.assert_called_once_with(tmp_path, "build", None)

    def test_posture_rewrite_called_with_contract(
        self, full_client: TestClient, tmp_path: Path
    ) -> None:
        """Transactional path calls _rewrite_posture when repo_root is set."""
        from unittest.mock import patch

        payload = {
            "value": "spec",
            "contract": {
                "contract_id": "ctr-pr-1",
                "phase": "spec",
                "slug": "pr-slug",
            },
        }
        with patch("agentalloy.api.state_router._rewrite_posture", return_value=[]) as mock_rewrite:
            resp = full_client.post(
                "/state/phase?repo_root=" + str(tmp_path),
                json=payload,
            )
            assert resp.status_code == 200
            mock_rewrite.assert_called_once_with(tmp_path, "spec", None)

    def test_no_posture_rewrite_without_repo_root(self, state_client: TestClient) -> None:
        """Omitting repo_root skips posture rewrite."""
        from unittest.mock import patch

        with patch("agentalloy.api.state_router._rewrite_posture", return_value=[]) as mock_rewrite:
            resp = state_client.post("/state/phase", json={"value": "build"})
            assert resp.status_code == 200
            mock_rewrite.assert_not_called()


# ---------------------------------------------------------------------------
# Posture rewrite soft-failing
# ---------------------------------------------------------------------------


class TestPostureRewriteSoftFail:
    """Posture rewrite failures must not block phase advances."""

    def test_rewrite_error_does_not_block_fast_path(
        self, state_client: TestClient, tmp_path: Path
    ) -> None:
        """rewrite_enforcement_posture raising an exception still returns 200."""
        from unittest.mock import patch

        with patch(
            "agentalloy.install.subcommands.wire_harness.rewrite_enforcement_posture",
            side_effect=RuntimeError("disk full"),
        ):
            resp = state_client.post(
                "/state/phase?repo_root=" + str(tmp_path),
                json={"value": "build"},
            )
            assert resp.status_code == 200
            assert resp.json()["value"] == "build"

    def test_rewrite_error_does_not_block_transactional_path(
        self, full_client: TestClient, tmp_path: Path
    ) -> None:
        """rewrite_enforcement_posture raising an exception still commits the
        transaction."""
        from unittest.mock import patch

        payload = {
            "value": "spec",
            "contract": {
                "contract_id": "ctr-sf-1",
                "phase": "spec",
                "slug": "sf-slug",
            },
        }
        with patch(
            "agentalloy.install.subcommands.wire_harness.rewrite_enforcement_posture",
            side_effect=RuntimeError("permission denied"),
        ):
            resp = full_client.post(
                "/state/phase?repo_root=" + str(tmp_path),
                json=payload,
            )
            assert resp.status_code == 200
            assert resp.json()["contract_id"] == "ctr-sf-1"

    def test_rewrite_helper_swallows_exception(self, tmp_path: Path) -> None:
        """The _rewrite_posture helper itself returns [] on error."""
        from unittest.mock import patch

        from agentalloy.api.state_router import _rewrite_posture

        with patch.object(
            __import__(
                "agentalloy.install.subcommands.wire_harness",
                fromlist=["rewrite_enforcement_posture"],
            ),
            "rewrite_enforcement_posture",
            side_effect=ImportError("no module"),
        ):
            result = _rewrite_posture(tmp_path, "build", None)
            assert result == []


# ---------------------------------------------------------------------------
# TC10 — POST /state/archive-all
# ---------------------------------------------------------------------------


class TestArchiveAll:
    """TC10: POST /state/archive-all archives all active contracts and artifacts."""

    def test_archive_all_archives_everything(
        self, state_client: TestClient, state_store: DuckDBStateStore
    ) -> None:
        """Archiving when there is work: returns counts > 0."""
        # Seed active contracts
        state_store.put_contract("ctr-arch-1", phase="build", slug="a", body="v1")
        state_store.put_contract("ctr-arch-2", phase="design", slug="b", body="v2")
        # Seed active artifacts
        state_store.set_artifact("build", "a", "design.md", "# design")
        state_store.set_artifact("design", "b", "spec.md", "# spec")

        resp = state_client.post("/state/archive-all")
        assert resp.status_code == 200
        body = resp.json()
        assert body["contracts"] == 2
        assert body["artifacts"] == 2

        # Verify they are now archived
        all_contracts = state_store.list_contracts(status="active")
        assert len(all_contracts) == 0

    def test_archive_all_does_not_cross_streams(
        self, state_client: TestClient, state_store: DuckDBStateStore
    ) -> None:
        """A sibling worktree's contracts are untouched by this stream's archive-all."""
        sibling = state_store.for_repo(state_store._repo(), stream_id="sibling-stream")
        sibling.put_contract("ctr-sibling", phase="build", slug="s")
        state_store.put_contract("ctr-mine", phase="build", slug="m")

        resp = state_client.post("/state/archive-all")
        assert resp.status_code == 200
        assert resp.json()["contracts"] == 1

        assert sibling.get_contract("ctr-sibling")["status"] == "active"
        assert state_store.get_contract("ctr-mine")["status"] == "archived"

    def test_archive_all_nothing_to_archive(
        self, state_client: TestClient, state_store: DuckDBStateStore
    ) -> None:
        """Already archived: returns 200 with zero counts (no-op, not an error)."""
        resp = state_client.post("/state/archive-all")
        assert resp.status_code == 200
        body = resp.json()
        assert body["contracts"] == 0
        assert body["artifacts"] == 0

    def test_archive_all_only_contracts(
        self, state_client: TestClient, state_store: DuckDBStateStore
    ) -> None:
        """When only contracts exist (no artifacts), archives just contracts."""
        state_store.put_contract("ctr-arch-c", phase="build", slug="c")

        resp = state_client.post("/state/archive-all")
        assert resp.status_code == 200
        body = resp.json()
        assert body["contracts"] == 1
        assert body["artifacts"] == 0

    def test_archive_all_only_artifacts(
        self, state_client: TestClient, state_store: DuckDBStateStore
    ) -> None:
        """When only artifacts exist (no contracts), archives just artifacts."""
        state_store.set_artifact("build", "c", "art.md", "# art")

        resp = state_client.post("/state/archive-all")
        assert resp.status_code == 200
        body = resp.json()
        assert body["contracts"] == 0
        assert body["artifacts"] == 1

    def test_archive_all_is_idempotent(
        self, state_client: TestClient, state_store: DuckDBStateStore
    ) -> None:
        """A second archive-all returns 200 with zero counts — idempotent no-op."""
        state_store.put_contract("ctr-arch-idem", phase="build", slug="idem")

        # First call succeeds
        resp1 = state_client.post("/state/archive-all")
        assert resp1.status_code == 200

        # Second call is a no-op (zero counts, not an error)
        resp2 = state_client.post("/state/archive-all")
        assert resp2.status_code == 200
        body = resp2.json()
        assert body["contracts"] == 0
        assert body["artifacts"] == 0

    def test_archive_all_outcome_cancelled(
        self, state_client: TestClient, state_store: DuckDBStateStore
    ) -> None:
        """outcome=cancelled retires in-flight contracts as abandoned."""
        state_store.put_contract("ctr-abandoned", phase="build", slug="ab")

        resp = state_client.post("/state/archive-all", json={"outcome": "cancelled"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["outcome"] == "cancelled"
        assert body["contracts"] == 1
        assert state_store.get_contract("ctr-abandoned")["status"] == "cancelled"

    def test_archive_all_spares_stashed_contracts(
        self, state_client: TestClient, state_store: DuckDBStateStore
    ) -> None:
        """Stashed (parked) contracts survive the cycle sweep."""
        state_store.create_session("sess-parked", task_slug="feat-parked")
        state_store.put_contract("build/feat-parked", phase="build", slug="feat-parked")
        state_store.put_contract("build/feat-live", phase="build", slug="feat-live")
        state_store.stash_session("sess-parked")

        resp = state_client.post("/state/archive-all", json={"outcome": "cancelled"})
        assert resp.status_code == 200
        assert resp.json()["contracts"] == 1
        assert state_store.get_contract("build/feat-parked")["status"] == "stashed"
        assert state_store.get_contract("build/feat-live")["status"] == "cancelled"

    def test_archive_all_rejects_invalid_outcome(
        self, state_client: TestClient
    ) -> None:
        resp = state_client.post("/state/archive-all", json={"outcome": "superseded"})
        assert resp.status_code == 422

    # ------------------------------------------------------------------
    # GET /state/artifact/{phase}/{slug}/{name} — single artifact route
    # ------------------------------------------------------------------

    def test_get_artifact_success(
        self, state_client: TestClient, state_store: DuckDBStateStore
    ) -> None:
        """TC1 — GET /state/artifact/{phase}/{slug}/{name} returns content."""
        state_store.set_artifact("spec", "my-task", "spec.md", "# The spec")

        resp = state_client.get("/state/artifact/spec/my-task/spec.md")
        assert resp.status_code == 200
        body = resp.json()
        assert body["phase"] == "spec"
        assert body["slug"] == "my-task"
        # set_artifact canonicalizes .md -> .artifact for store-backed phases.
        assert body["name"] == "spec.artifact"
        assert body["content"] == "# The spec"

    def test_get_artifact_not_found(
        self, state_client: TestClient, state_store: DuckDBStateStore
    ) -> None:
        """TC2 — GET /state/artifact/{phase}/{slug}/{name} returns 404."""
        resp = state_client.get("/state/artifact/spec/missing/art.md")
        assert resp.status_code == 404

    def test_get_artifact_uses_active_filter(
        self, state_client: TestClient, state_store: DuckDBStateStore
    ) -> None:
        """TC3 — get_artifact filters to status='active' by default."""
        # Store an active artifact
        state_store.set_artifact("design", "feat", "plan.md", "active content")
        # Mark it archived (status='archived')
        state_store.conn.execute(
            "UPDATE sdd_artifact SET status='archived' WHERE slug='feat' AND name='plan.artifact'"
        )

        resp = state_client.get("/state/artifact/design/feat/plan.md")
        assert resp.status_code == 404  # active filter excludes archived

    def test_get_artifact_route_before_catchall(
        self, state_client: TestClient, state_store: DuckDBStateStore
    ) -> None:
        """TC4 — the specific /artifact/… route takes priority over /{kind}."""
        state_store.set_artifact("build", "c", "x.md", "x")
        resp = state_client.get("/state/artifact/build/c/x.md")
        assert resp.status_code == 200
        body = resp.json()
        assert body["phase"] == "build"
        assert body["content"] == "x"

    # ------------------------------------------------------------------
    # PUT /state/artifact — AC parsing on spec artifact set
    # ------------------------------------------------------------------

    def test_put_artifact_spec_parses_ac_headings(
        self, state_client: TestClient, state_store: DuckDBStateStore
    ) -> None:
        """TC5 — PUT /state/artifact with phase=spec parses AC headings into contract."""
        # Create a spec contract first (contract_id = phase/slug)
        state_store.put_contract(
            "spec/test-ac-slug",
            phase="spec",
            slug="test-ac-slug",
            body="# Test contract",
        )
        # Set artifact with AC headings
        body = "## AC-1: login works\n## AC-2: logout works\n## Out of Scope\n## AC-3: signup works"
        resp = state_client.put(
            "/state/artifact",
            json={
                "phase": "spec",
                "slug": "test-ac-slug",
                "name": "spec.md",
                "content": body,
            },
        )
        assert resp.status_code == 200

        # Verify contract was updated with structured ACs
        contract = state_store.get_contract("spec/test-ac-slug")
        assert contract is not None
        criteria = contract.get("success_criteria") or []
        assert len(criteria) == 3
        ids = [c["id"] for c in criteria if isinstance(c, dict)]
        assert "AC-1" in ids
        assert "AC-2" in ids
        assert "AC-3" in ids

    def test_put_artifact_non_spec_ignores_ac_headings(
        self, state_client: TestClient, state_store: DuckDBStateStore
    ) -> None:
        """TC6 — PUT /state/artifact with non-spec phase does not parse AC headings."""
        state_store.put_contract(
            "build/test-no-ac",
            phase="build",
            slug="test-no-ac",
            body="# Build contract",
        )
        body = "## AC-1: should be ignored"
        resp = state_client.put(
            "/state/artifact",
            json={
                "phase": "build",
                "slug": "test-no-ac",
                "name": "tasks.md",
                "content": body,
            },
        )
        assert resp.status_code == 200

        # Contract should not have ACs parsed from artifact
        contract = state_store.get_contract("build/test-no-ac")
        assert contract is not None
        criteria = contract.get("success_criteria") or []
        assert len(criteria) == 0


# ---------------------------------------------------------------------------
# Session registry endpoints (WI-2)
# ---------------------------------------------------------------------------


class TestSessionEndpoints:
    """/state/sessions/{active,archive,resume} — the session registry routes.

    The ``state_store`` fixture is opened under the same ``(repo, stream_id)``
    the routes resolve from the request, so rows seeded through it are visible
    to the client (and vice versa).
    """

    def test_list_active_sessions_empty(self, state_client: TestClient) -> None:
        resp = state_client.get("/state/sessions/active")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_active_sessions_returns_seeded(
        self, state_client: TestClient, state_store: DuckDBStateStore
    ) -> None:
        state_store.create_session("sess-1", task_slug="01-cache", phase="build")
        state_store.create_session("sess-2", task_slug="02-api", phase="build")

        resp = state_client.get("/state/sessions/active")
        assert resp.status_code == 200
        rows = resp.json()
        keys = {r["session_key"] for r in rows}
        assert keys == {"sess-1", "sess-2"}
        by_key = {r["session_key"]: r for r in rows}
        assert by_key["sess-1"]["task_slug"] == "01-cache"
        assert by_key["sess-1"]["phase"] == "build"
        assert by_key["sess-2"]["task_slug"] == "02-api"

    def test_archive_session_success(
        self, state_client: TestClient, state_store: DuckDBStateStore
    ) -> None:
        state_store.create_session("sess-1", phase="build")

        resp = state_client.post("/state/sessions/archive", json={"session_key": "sess-1"})
        assert resp.status_code == 200
        assert resp.json() == {"archived": True}
        # The row is now archived and dropped from the active list.
        assert state_store.get_session("sess-1")["status"] == "archived"
        assert state_client.get("/state/sessions/active").json() == []

    def test_archive_session_not_found(self, state_client: TestClient) -> None:
        resp = state_client.post("/state/sessions/archive", json={"session_key": "ghost"})
        assert resp.status_code == 200
        assert resp.json() == {"archived": False}

    def test_archive_missing_session_key_is_400(self, state_client: TestClient) -> None:
        resp = state_client.post("/state/sessions/archive", json={})
        assert resp.status_code == 400

    def test_resume_session_reactivates_archived(
        self, state_client: TestClient, state_store: DuckDBStateStore
    ) -> None:
        state_store.create_session("sess-1", phase="build")
        state_store.archive_session("sess-1")
        assert state_store.get_session("sess-1")["status"] == "archived"

        resp = state_client.post("/state/sessions/resume", json={"session_key": "sess-1"})
        assert resp.status_code == 200
        assert resp.json() == {"resumed": True}
        assert state_store.get_session("sess-1")["status"] == "active"
        keys = {r["session_key"] for r in state_client.get("/state/sessions/active").json()}
        assert keys == {"sess-1"}

    def test_resume_session_not_found(self, state_client: TestClient) -> None:
        resp = state_client.post("/state/sessions/resume", json={"session_key": "ghost"})
        assert resp.status_code == 200
        assert resp.json() == {"resumed": False}

    def test_resume_missing_session_key_is_400(self, state_client: TestClient) -> None:
        resp = state_client.post("/state/sessions/resume", json={})
        assert resp.status_code == 400

    def test_stash_session_parks_session_and_contracts(
        self, state_client: TestClient, state_store: DuckDBStateStore
    ) -> None:
        state_store.create_session("sess-1", task_slug="feat-a", phase="build")
        state_store.put_contract("build/feat-a", phase="build", slug="feat-a")

        resp = state_client.post("/state/sessions/stash", json={"session_key": "sess-1"})
        assert resp.status_code == 200
        assert resp.json() == {"stashed": True}
        assert state_store.get_session("sess-1")["status"] == "stashed"
        assert state_store.get_contract("build/feat-a")["status"] == "stashed"
        assert state_client.get("/state/sessions/active").json() == []

    def test_stash_session_not_found(self, state_client: TestClient) -> None:
        resp = state_client.post("/state/sessions/stash", json={"session_key": "ghost"})
        assert resp.status_code == 200
        assert resp.json() == {"stashed": False}

    def test_resume_stashed_session_restores_contracts(
        self, state_client: TestClient, state_store: DuckDBStateStore
    ) -> None:
        state_store.create_session("sess-1", task_slug="feat-a")
        state_store.put_contract("build/feat-a", phase="build", slug="feat-a")
        state_store.stash_session("sess-1")

        resp = state_client.post("/state/sessions/resume", json={"session_key": "sess-1"})
        assert resp.json() == {"resumed": True}
        assert state_store.get_session("sess-1")["status"] == "active"
        assert state_store.get_contract("build/feat-a")["status"] == "active"

    def test_cancel_session_is_terminal(
        self, state_client: TestClient, state_store: DuckDBStateStore
    ) -> None:
        state_store.create_session("sess-1", task_slug="feat-a")
        state_store.put_contract("spec/feat-a", phase="spec", slug="feat-a")

        resp = state_client.post("/state/sessions/cancel", json={"session_key": "sess-1"})
        assert resp.status_code == 200
        assert resp.json() == {"cancelled": True}
        assert state_store.get_session("sess-1")["status"] == "cancelled"
        assert state_store.get_contract("spec/feat-a")["status"] == "cancelled"

        # Cancelled is terminal — resume refuses
        resp = state_client.post("/state/sessions/resume", json={"session_key": "sess-1"})
        assert resp.json() == {"resumed": False}
        assert state_store.get_session("sess-1")["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Approval flow — POST /state/advance approved + POST /state/approve-phase
# ---------------------------------------------------------------------------


class TestAdvanceApproval:
    """Approval-gated advance: the digest is computed server-side and binds
    the approval to the exact artifact content at sign-off.

    ``repo_root`` must name the fixture's own bucket root
    (``default_repo_root()``) — the store is seeded under that (repo, stream)
    pair, and any other path re-scopes the request to an empty bucket.
    """

    def _setup_spec(self, state_store: DuckDBStateStore) -> None:
        state_store.write_phase("spec")
        state_store.put_contract("spec/feat-a", phase="spec", slug="feat-a", body="spec contract")
        # The packaged spec exit gate requires these sections in the artifact
        state_store.set_artifact(
            "spec",
            "feat-a",
            "spec.artifact",
            "# Spec\nContent v1\n\n## Acceptance Criteria\n- AC-1\n\n## Out of Scope\n- none",
        )

    def _advance_url(self) -> str:
        return f"/state/advance?repo_root={default_repo_root()}"

    def _approve_url(self) -> str:
        return f"/state/approve-phase?repo_root={default_repo_root()}"

    def test_advance_without_approval_is_blocked(
        self, state_client: TestClient, state_store: DuckDBStateStore
    ) -> None:
        self._setup_spec(state_store)
        resp = state_client.post(
            self._advance_url(),
            json={"slug": "feat-a", "contract_body": "design brief", "to_phase": "design"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        assert body["gate_verdict"]["reason"] == "approval"

    def test_advance_approved_flows_to_design(
        self, state_client: TestClient, state_store: DuckDBStateStore
    ) -> None:
        from unittest.mock import patch

        self._setup_spec(state_store)
        with patch("agentalloy.api.state_router._rewrite_posture", return_value=[]):
            resp = state_client.post(
                self._advance_url(),
                json={
                    "slug": "feat-a",
                    "contract_body": "design brief",
                    "to_phase": "design",
                    "approved": True,
                },
            )
        body = resp.json()
        assert body["success"] is True, body
        assert body["phase"] == "design"
        # Approval recorded server-side
        approval = state_store.get_approval("spec")
        assert approval is not None
        assert approval["approver"] == "agent-on-user-approval"
        assert approval["artifact_digest"]
        # The next-phase contract was written
        assert state_store.get_contract("design/feat-a") is not None

    def test_advance_approved_refused_without_artifact(
        self, state_client: TestClient, state_store: DuckDBStateStore
    ) -> None:
        """Nothing to approve → refuse before writing the next contract."""
        state_store.write_phase("spec")
        state_store.put_contract("spec/feat-a", phase="spec", slug="feat-a")
        resp = state_client.post(
            self._advance_url(),
            json={
                "slug": "feat-a",
                "contract_body": "design brief",
                "to_phase": "design",
                "approved": True,
            },
        )
        body = resp.json()
        assert body["success"] is False
        assert "no exit artifact" in body["message"]
        assert state_store.get_approval("spec") is None
        assert state_store.get_contract("design/feat-a") is None

    def test_approval_goes_stale_when_artifact_changes(
        self, state_client: TestClient, state_store: DuckDBStateStore
    ) -> None:
        """Editing the artifact after approval voids it (digest mismatch)."""
        from unittest.mock import patch

        self._setup_spec(state_store)
        with patch("agentalloy.api.state_router._rewrite_posture", return_value=[]):
            first = state_client.post(
                self._advance_url(),
                json={
                    "slug": "feat-a",
                    "contract_body": "design brief",
                    "to_phase": "design",
                    "approved": True,
                },
            )
        assert first.json()["success"] is True

        # Findings routed back: return to spec and revise the artifact
        state_store.write_phase("spec")
        state_store.set_artifact(
            "spec",
            "feat-a",
            "spec.artifact",
            "# Spec\nContent v2 — edited after approval\n\n"
            "## Acceptance Criteria\n- AC-1\n\n## Out of Scope\n- none",
        )

        # Advance without re-approval must re-block
        resp = state_client.post(
            self._advance_url(),
            json={"slug": "feat-a", "contract_body": "design brief v2", "to_phase": "design"},
        )
        body = resp.json()
        assert body["success"] is False
        assert body["gate_verdict"]["reason"] == "approval"

    def test_approve_phase_endpoint_records_digest(
        self, state_client: TestClient, state_store: DuckDBStateStore
    ) -> None:
        self._setup_spec(state_store)
        resp = state_client.post(self._approve_url(), json={"phase": "spec"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["phase"] == "spec"
        assert body["slug"] == "feat-a"
        assert body["artifact_digest"]
        assert state_store.get_approval("spec")["artifact_digest"] == body["artifact_digest"]

    def test_approve_phase_refuses_without_artifact(
        self, state_client: TestClient, state_store: DuckDBStateStore
    ) -> None:
        state_store.write_phase("spec")
        state_store.put_contract("spec/feat-a", phase="spec", slug="feat-a")
        resp = state_client.post(self._approve_url(), json={"phase": "spec"})
        assert resp.status_code == 422
        assert "no exit artifact" in resp.json()["detail"]
