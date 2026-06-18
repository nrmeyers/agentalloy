"""Tests for the savings telemetry path (2.3.4).

Covers the new `GET /telemetry/savings` endpoint and the CLI's API-vs-direct
routing — the CLI must NOT open the DuckDB directly while the service is up
(the service holds the single read-write lock).
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentalloy.api.telemetry_router import TelemetryQuerier, router
from agentalloy.install.subcommands import telemetry
from agentalloy.storage.vector_store import CompositionTrace, open_or_create


def _client(querier: TelemetryQuerier | None) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    if querier is not None:
        app.state.telemetry_querier = querier
    return TestClient(app)


def _compose_trace(trace_id: str, phase: str, returned: int, flat: int) -> CompositionTrace:
    return CompositionTrace(
        trace_id=trace_id,
        request_ts=int(time.time()),
        phase=phase,
        task_prompt="t",
        status="compose",
        tokens_returned=returned,
        tokens_flat_equivalent=flat,
    )


class TestSavingsEndpoint:
    def test_no_querier_returns_zeros(self) -> None:
        resp = _client(None).get("/telemetry/savings")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_composes"] == 0
        assert data["per_phase"] == []

    def test_aggregates_seeded_traces(self, tmp_path: Path) -> None:
        store = open_or_create(tmp_path / "t.duck")
        try:
            store.record_composition_trace(_compose_trace("a", "build", 100, 400))
            store.record_composition_trace(_compose_trace("b", "spec", 50, 150))
            resp = _client(TelemetryQuerier(store)).get("/telemetry/savings")
        finally:
            store.close()
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_composes"] == 2
        assert data["tokens_returned"] == 150
        assert data["tokens_flat_equivalent"] == 550
        assert data["tokens_saved"] == 400
        phases = {p["phase"] for p in data["per_phase"]}
        assert phases == {"build", "spec"}


def _args() -> argparse.Namespace:
    return argparse.Namespace(json=False, confirm=True)


_SAVINGS = {
    "total_composes": 1,
    "tokens_returned": 10,
    "tokens_flat_equivalent": 40,
    "tokens_saved": 30,
    "savings_pct": 75.0,
    "per_phase": [],
}


class TestSavingsCliRouting:
    def test_uses_api_when_service_up(self) -> None:
        with (
            patch("agentalloy.install.subcommands.telemetry._service_port", return_value=47950),
            patch("agentalloy.install.server_proc.port_reachable", return_value=True),
            patch(
                "agentalloy.install.subcommands.telemetry._fetch_savings_via_api",
                return_value=_SAVINGS,
            ),
            patch("agentalloy.storage.vector_store.open_or_create") as mock_open,
        ):
            rc = telemetry._run_savings(_args())
        assert rc == 0
        mock_open.assert_not_called()  # API used; DB never opened

    def test_errors_when_service_up_but_api_silent(self) -> None:
        with (
            patch("agentalloy.install.subcommands.telemetry._service_port", return_value=47950),
            patch("agentalloy.install.server_proc.port_reachable", return_value=True),
            patch(
                "agentalloy.install.subcommands.telemetry._fetch_savings_via_api",
                return_value=None,
            ),
            patch("agentalloy.storage.vector_store.open_or_create") as mock_open,
        ):
            rc = telemetry._run_savings(_args())
        assert rc == 1
        mock_open.assert_not_called()  # never risk the lock

    def test_direct_db_when_service_down(self) -> None:
        fake_vs = MagicMock()
        fake_vs.aggregate_savings.return_value = _SAVINGS
        with (
            patch("agentalloy.install.subcommands.telemetry._service_port", return_value=47950),
            patch("agentalloy.install.server_proc.port_reachable", return_value=False),
            patch(
                "agentalloy.storage.vector_store.open_or_create", return_value=fake_vs
            ) as mock_open,
        ):
            rc = telemetry._run_savings(_args())
        assert rc == 0
        mock_open.assert_called_once()
        fake_vs.aggregate_savings.assert_called_once()


class TestClearLockGuard:
    def test_clear_refuses_when_service_up(self) -> None:
        with (
            patch("agentalloy.install.subcommands.telemetry._service_port", return_value=47950),
            patch("agentalloy.install.server_proc.port_reachable", return_value=True),
            patch("agentalloy.storage.vector_store.open_or_create") as mock_open,
        ):
            rc = telemetry._run_clear(_args())
        assert rc == 1
        mock_open.assert_not_called()  # clean message instead of a DuckDB traceback
