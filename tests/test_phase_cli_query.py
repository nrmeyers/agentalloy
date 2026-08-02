"""Tests for `agentalloy telemetry phases` (Task 05 of add-telemetry).

T17 — basic query returns per-phase counts.
T18 — --phase filter scopes results.
T19 — --event-type filter drives the latency aggregation.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import MagicMock, patch

from agentalloy.install.subcommands import telemetry
from agentalloy.storage.telemetry_store import open_telemetry_store
from agentalloy.telemetry.phase_writer import PhaseTelemetryWriter


def _args(**overrides: object) -> argparse.Namespace:
    base = dict(
        phase=None,
        event_type=None,
        since=None,
        until=None,
        limit=20,
        json=True,
        quiet=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _seed(db_path: Path) -> None:
    store = open_telemetry_store(db_path)
    try:
        writer = PhaseTelemetryWriter(store)
        writer.phase_start("t1", "design", workflow_skill_id="sdd-design")
        writer.llm_sent("t1", "design", model="qwen3-235b")
        writer.llm_received("t1", "design", model="qwen3-235b", tokens_out=1500, latency_ms=1000)
        writer.phase_complete("t1", "design", latency_ms=1100, success=True)

        writer.phase_start("t2", "build", workflow_skill_id="sdd-build")
        writer.llm_sent("t2", "build", model="qwen3-235b")
        writer.llm_received("t2", "build", model="qwen3-235b", tokens_out=3000, latency_ms=2000)
        writer.llm_error("t2", "build", model="qwen3-235b", error_message="timeout", latency_ms=4000)
    finally:
        store.close()


class TestPhasesCliParsing:
    def _parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(prog="agentalloy")
        sub = parser.add_subparsers()
        telemetry.add_parser(sub)
        return parser

    def test_phases_is_a_registered_subcommand(self) -> None:
        args = self._parser().parse_args(["telemetry", "phases"])
        assert args.func is telemetry._run_phases  # pyright: ignore[reportPrivateUsage]

    def test_flags_parse(self) -> None:
        args = self._parser().parse_args(
            [
                "telemetry",
                "phases",
                "--phase",
                "design",
                "--event-type",
                "llm_received",
                "--since",
                "100",
                "--until",
                "200",
                "--limit",
                "5",
                "--json",
            ]
        )
        assert args.phase == "design"
        assert args.event_type == "llm_received"
        assert args.since == 100
        assert args.until == 200
        assert args.limit == 5
        assert args.json is True


class TestPhasesCliLockGuard:
    def test_refuses_when_service_up(self) -> None:
        with (
            patch("agentalloy.install.subcommands.telemetry._service_port", return_value=47950),
            patch("agentalloy.install.server_proc.port_reachable", return_value=True),
            patch("agentalloy.storage.open.open_telemetry") as mock_open,
        ):
            rc = telemetry._run_phases(_args())
        assert rc == 1
        mock_open.assert_not_called()

    def test_fresh_install_no_db_file(self, tmp_path: Path) -> None:
        settings = MagicMock(telemetry_db_path=str(tmp_path / "telemetry.duck"))
        with (
            patch("agentalloy.install.subcommands.telemetry._service_port", return_value=47950),
            patch("agentalloy.install.server_proc.port_reachable", return_value=False),
            patch("agentalloy.config.get_settings", return_value=settings),
            patch("agentalloy.storage.open.open_telemetry") as mock_open,
        ):
            rc = telemetry._run_phases(_args())
        assert rc == 0
        mock_open.assert_not_called()


class TestPhasesQuery:
    """T17-T19 against a real seeded DuckDB (service down -> direct open)."""

    def _run(self, db_path: Path, **arg_overrides: object) -> dict:
        settings = MagicMock(telemetry_db_path=str(db_path))
        with (
            patch("agentalloy.install.subcommands.telemetry._service_port", return_value=47950),
            patch("agentalloy.install.server_proc.port_reachable", return_value=False),
            patch("agentalloy.config.get_settings", return_value=settings),
            patch("agentalloy.install.subcommands.telemetry.write_result") as mock_write,
        ):
            rc = telemetry._run_phases(_args(**arg_overrides))
        assert rc == 0
        return mock_write.call_args[0][0]

    def test_t17_basic_query_returns_events(self, tmp_path: Path) -> None:
        db_path = tmp_path / "telemetry.duck"
        _seed(db_path)
        result = self._run(db_path)
        phases = {row["phase"] for row in result["per_phase"]}
        assert phases == {"design", "build"}
        assert len(result["timeline"]) > 0

    def test_t18_filter_by_phase(self, tmp_path: Path) -> None:
        db_path = tmp_path / "telemetry.duck"
        _seed(db_path)
        result = self._run(db_path, phase="design")
        phases = {row["phase"] for row in result["per_phase"]}
        assert phases == {"design"}
        assert all(row["phase"] == "design" for row in result["timeline"])

    def test_t19_latency_aggregation(self, tmp_path: Path) -> None:
        db_path = tmp_path / "telemetry.duck"
        _seed(db_path)
        result = self._run(db_path, event_type="llm_received")
        latency = result["llm_latency"]
        assert latency["count"] == 2
        assert latency["avg_latency_ms"] == 1500.0
        assert latency["p95_latency_ms"] is not None
        assert all(row["event_type"] == "llm_received" for row in result["timeline"])

    def test_empty_db_returns_empty_result(self, tmp_path: Path) -> None:
        db_path = tmp_path / "telemetry.duck"
        store = open_telemetry_store(db_path)
        store.close()
        result = self._run(db_path)
        assert result["per_phase"] == []
        assert result["timeline"] == []
        assert result["llm_latency"]["count"] == 0
