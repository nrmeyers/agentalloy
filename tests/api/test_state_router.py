"""Tests for the state router, client cutover, and in-process invariant.

Test cases from docs/design/contract-store-and-write-gating/test-plan.md:

- TA2 — no ``duckdb.connect`` in the install/web import graph for state paths
- TA5 — service down: StateClient raises StateClientError naming the service
- TE2 — compose opens no HTTP connection for state
"""

from __future__ import annotations

import importlib
import inspect
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentalloy.api.state_client import StateClient, StateClientError
from agentalloy.api.state_router import (
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
    store = open_state_store(db)
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

    def test_lease_conflict_on_phase(
        self, state_client: TestClient, state_store: DuckDBStateStore
    ) -> None:
        from datetime import datetime, timedelta

        # Set up a row with an active lease directly via SQL (work around
        # pre-existing bug in acquire_lease INSERT param count).
        now = datetime.now()
        future = (now + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        ts = now.strftime("%Y-%m-%d %H:%M:%S")
        repo = state_store._repo()
        state_store.conn.execute(
            "INSERT INTO sdd_state "
            "(repo, kind, session_key, value, owner, updated_at, lease_expires_at) "
            "VALUES (?, ?, '', 'spec', ?, ?, ?)",
            (repo, "phase", "s1", ts, future),
        )
        # Session s2 tries to write — should get 409.
        resp = state_client.post("/state/phase", json={"value": "build", "owner": "s2"})
        assert resp.status_code == 409
        body = resp.json()
        detail = body["detail"]
        assert detail["owner"] == "s1"
        assert "lease_expires_at" in detail
        assert "s1" in detail["message"]


# ---------------------------------------------------------------------------
# TA2 — no duckdb.connect in install/web import graph
# ---------------------------------------------------------------------------


class TestTA2:
    """TA2: Every out-of-process caller path (CLI, web) goes through HTTP —
    assert no ``duckdb.connect`` in install/web import graph for state paths.

    The state-related CLI subcommands (phase, approve, task, flow) must not
    open DuckDB directly; they route through StateClient → HTTP.  Other
    install subcommands (customize, update, contract) may still use DuckDB
    for non-state operations.
    """

    def test_state_subcommands_no_duckdb_connect(self) -> None:
        """State-related install subcommands do not call duckdb.connect."""
        state_modules = [
            "agentalloy.install.subcommands.phase",
            "agentalloy.install.subcommands.approve",
            "agentalloy.install.subcommands.task",
            "agentalloy.install.subcommands.flow",
        ]
        for mod_name in state_modules:
            mod = importlib.import_module(mod_name)
            source = inspect.getsource(mod)
            assert "duckdb.connect" not in source, (
                f"{mod_name} contains duckdb.connect — state mutations must go through HTTP"
            )

    def test_web_modules_no_duckdb_connect(self) -> None:
        """Web API modules do not call duckdb.connect."""
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
        """StateClient itself does not import duckdb."""
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
        """set_phase raises StateClientError with service name."""
        client = StateClient(base_url="http://127.0.0.1:19999")
        with pytest.raises(StateClientError) as exc_info:
            client.set_phase("build")
        assert "agentalloy service" in exc_info.value.message

    def test_approve_raises_when_service_down(self) -> None:
        """approve raises StateClientError with service name."""
        client = StateClient(base_url="http://127.0.0.1:19988")
        with pytest.raises(StateClientError) as exc_info:
            client.approve("spec")
        assert "agentalloy service" in exc_info.value.message

    def test_set_cursor_raises_when_service_down(self) -> None:
        """set_cursor raises StateClientError with service name."""
        client = StateClient(base_url="http://127.0.0.1:19997")
        with pytest.raises(StateClientError) as exc_info:
            client.set_cursor("active/build/01.md")
        assert "agentalloy service" in exc_info.value.message

    def test_no_file_written_when_service_down(self, tmp_path: Path) -> None:
        """A failed set_phase does not create any files."""
        client = StateClient(base_url="http://127.0.0.1:19996")
        phase_file = tmp_path / ".agentalloy" / "phase"
        phase_file.parent.mkdir(parents=True)
        with pytest.raises(StateClientError):
            client.set_phase("build")
        assert not phase_file.exists()

    def test_no_fallback_methods_exist(self) -> None:
        """_read_phase_file and _write_phase_file have been removed."""
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
        """ComposeOrchestrator does not import StateClient."""
        mod = importlib.import_module("agentalloy.orchestration.compose")
        source = inspect.getsource(mod)
        assert "state_client" not in source.lower()
        assert "StateClient" not in source

    def test_compose_router_no_state_client_import(self) -> None:
        """compose_router does not import StateClient."""
        mod = importlib.import_module("agentalloy.api.compose_router")
        source = inspect.getsource(mod)
        assert "state_client" not in source.lower()
        assert "StateClient" not in source

    def test_proxy_signal_no_state_client_import(self) -> None:
        """proxy_signal does not import StateClient for state operations."""
        mod = importlib.import_module("agentalloy.api.proxy_signal")
        source = inspect.getsource(mod)
        assert "StateClient" not in source

    def test_signals_module_no_state_client_import(self) -> None:
        """signals package does not import StateClient."""
        signals_pkg = importlib.import_module("agentalloy.signals")
        pkg_dir = Path(signals_pkg.__file__).parent
        for py_file in pkg_dir.glob("*.py"):
            if py_file.name.startswith("__"):
                continue
            source = py_file.read_text()
            assert "StateClient" not in source, (
                f"{py_file.name} imports StateClient — in-process paths must use StateStore directly"
            )
