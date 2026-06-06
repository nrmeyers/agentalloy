# ruff: noqa: I001 -- testing private module members intentionally
"""Tests for verify.py bootstrap detection (UT-31..UT-34, IT-6, IT-7)."""

from __future__ import annotations

import json
from unittest.mock import patch

import urllib.error

from agentalloy.install.subcommands import verify


def _fake_resp(body: dict) -> object:
    """Return a context-manager mimicking urllib.request.urlopen's response."""

    class _Resp:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        def read(self) -> bytes:
            return self._payload

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *_: object) -> None:
            return None

    return _Resp(json.dumps(body).encode())


class TestCheckBootstrapInProgress:
    def test_ut31_warming_up_returns_bootstrap_in_progress(self) -> None:
        with patch(
            "urllib.request.urlopen",
            return_value=_fake_resp({"status": "warming_up", "progress": {"packs_ingested": 1}}),
        ):
            result = verify._check_bootstrap_in_progress(47950)  # pyright: ignore[reportPrivateUsage]
        assert result is not None
        assert result["status"] == "bootstrap_in_progress"
        assert "warming up" in result["guidance"]
        assert result["all_checks_passed"] is False
        assert result["progress"] == {"packs_ingested": 1}

    def test_ut32_ready_returns_none(self) -> None:
        with patch("urllib.request.urlopen", return_value=_fake_resp({"status": "ready"})):
            result = verify._check_bootstrap_in_progress(47950)  # pyright: ignore[reportPrivateUsage]
        assert result is None

    def test_ut33_connection_error_returns_none(self) -> None:
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            result = verify._check_bootstrap_in_progress(47950)  # pyright: ignore[reportPrivateUsage]
        assert result is None

    def test_error_status_returns_bootstrap_error(self) -> None:
        with patch(
            "urllib.request.urlopen",
            return_value=_fake_resp({"status": "error", "progress": {"error": "stale_lock"}}),
        ):
            result = verify._check_bootstrap_in_progress(47950)  # pyright: ignore[reportPrivateUsage]
        assert result is not None
        assert result["status"] == "bootstrap_error"
        assert result["progress"] == {"error": "stale_lock"}

    def test_malformed_json_returns_none(self) -> None:
        class _BadResp:
            def read(self) -> bytes:
                return b"not json"

            def __enter__(self) -> _BadResp:
                return self

            def __exit__(self, *_: object) -> None:
                return None

        with patch("urllib.request.urlopen", return_value=_BadResp()):
            assert verify._check_bootstrap_in_progress(47950) is None  # pyright: ignore[reportPrivateUsage]


class TestRunChecksRouting:
    """[UT-34, IT-6, IT-7] run_checks short-circuits on warming_up only."""

    def test_ut34_run_checks_short_circuits_on_warming_up(self) -> None:
        st = {"deployment": "container", "port": 47950}
        with (
            patch(
                "urllib.request.urlopen",
                return_value=_fake_resp({"status": "warming_up", "progress": {}}),
            ),
            # Sentinel: if the full suite runs, this will be invoked.
            patch(
                "agentalloy.install.subcommands.verify._check_embedding_endpoint_reachable",
                side_effect=AssertionError("full suite should NOT run during bootstrap"),
            ),
        ):
            result = verify.run_checks(st)
        assert result["status"] == "bootstrap_in_progress"
        assert result["checks"] == []

    def test_it7_run_checks_proceeds_on_ready(self) -> None:
        """When /readiness=ready, the full suite runs. We only check that
        the bootstrap branch DOES NOT short-circuit (it returns None)."""
        st = {"deployment": "container", "port": 47950}
        with (
            patch("urllib.request.urlopen", return_value=_fake_resp({"status": "ready"})),
            # Stub the suite so we don't need real DBs / network.
            patch(
                "agentalloy.install.subcommands.verify._check_embedding_via_diagnostics",
                return_value={"name": "embed", "passed": True},
            ),
            patch(
                "agentalloy.install.subcommands.verify._check_duckdb_present",
                return_value={"name": "duck", "passed": True},
            ),
            patch(
                "agentalloy.install.subcommands.verify._check_ladybug_present",
                return_value={"name": "ladybug", "passed": True},
            ),
            patch(
                "agentalloy.install.subcommands.verify._check_skill_count",
                return_value={"name": "count", "passed": True},
            ),
            patch(
                "agentalloy.install.subcommands.verify._check_harness_config_present",
                return_value={"name": "harness", "passed": True},
            ),
            patch(
                "agentalloy.install.subcommands.verify._check_harness_config_url",
                return_value={"name": "url", "passed": True},
            ),
            patch(
                "agentalloy.install.subcommands.verify._check_port_available",
                return_value={"name": "port", "passed": True},
            ),
            patch("agentalloy.install.subcommands.verify._probe_diagnostics", return_value=None),
        ):
            result = verify.run_checks(st)
        assert result.get("status") != "bootstrap_in_progress"
        assert isinstance(result["checks"], list)
        assert len(result["checks"]) >= 1

    def test_native_deployment_skips_readiness_call(self) -> None:
        """Native installs don't have a /readiness endpoint to call."""
        st = {"deployment": "native", "port": 47950}
        urlopen_called = False

        def fake_urlopen(*_a, **_kw):  # type: ignore[no-untyped-def]
            nonlocal urlopen_called
            urlopen_called = True
            raise urllib.error.URLError("should not be called")

        with (
            patch("urllib.request.urlopen", side_effect=fake_urlopen),
            patch(
                "agentalloy.install.subcommands.verify._check_embedding_endpoint_reachable",
                return_value={"name": "embed", "passed": True},
            ),
            patch(
                "agentalloy.install.subcommands.verify._check_embedding_1024_dim",
                return_value={"name": "dim", "passed": True},
            ),
            patch(
                "agentalloy.install.subcommands.verify._check_duckdb_present",
                return_value={"name": "duck", "passed": True},
            ),
            patch(
                "agentalloy.install.subcommands.verify._check_ladybug_present",
                return_value={"name": "ladybug", "passed": True},
            ),
            patch(
                "agentalloy.install.subcommands.verify._check_skill_count",
                return_value={"name": "count", "passed": True},
            ),
            patch(
                "agentalloy.install.subcommands.verify._check_harness_config_present",
                return_value={"name": "harness", "passed": True},
            ),
            patch(
                "agentalloy.install.subcommands.verify._check_harness_config_url",
                return_value={"name": "url", "passed": True},
            ),
            patch(
                "agentalloy.install.subcommands.verify._check_port_available",
                return_value={"name": "port", "passed": True},
            ),
            patch("agentalloy.install.subcommands.verify._probe_diagnostics", return_value=None),
        ):
            verify.run_checks(st)
        assert urlopen_called is False
