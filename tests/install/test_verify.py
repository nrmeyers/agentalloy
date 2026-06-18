"""Unit tests for the ``verify`` subcommand.

Tests the individual check functions in isolation (mocked external deps).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.error import URLError

from agentalloy.install.subcommands.verify import (
    MIN_SKILL_COUNT,
    SCHEMA_VERSION,
    _check_duckdb_present,  # pyright: ignore[reportPrivateUsage]
    _check_embedding_endpoint_reachable,  # pyright: ignore[reportPrivateUsage]
    _check_embedding_expected_dim,  # pyright: ignore[reportPrivateUsage]
    _check_embedding_via_diagnostics,  # pyright: ignore[reportPrivateUsage]
    _check_harness_config_present,  # pyright: ignore[reportPrivateUsage]
    _check_harness_config_url,  # pyright: ignore[reportPrivateUsage]
    _check_ladybug_present,  # pyright: ignore[reportPrivateUsage]
    _check_port_available,  # pyright: ignore[reportPrivateUsage]
    _check_reranker_reachable,  # pyright: ignore[reportPrivateUsage]
    _check_skill_count,  # pyright: ignore[reportPrivateUsage]
    run_checks,
)


class TestRerankerCheck:
    """The reranker check is advisory — a down reranker WARNs, never FAILs (the
    signal layer falls back to the cosine floor)."""

    def test_cosine_backend_is_plain_pass(self) -> None:
        with patch.dict(os.environ, {"SIGNAL_INTENT_BACKEND": "cosine"}, clear=False):
            chk = _check_reranker_reachable({})
        assert chk["passed"] is True
        assert "severity" not in chk
        assert "cosine" in chk["detail"]

    def test_reachable_passes_without_warn(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "SIGNAL_INTENT_BACKEND": "reranker",
                    "SIGNAL_INTENT_RERANK_URL": "http://127.0.0.1:47952",
                },
                clear=False,
            ),
            patch("agentalloy.install.state.rerank_reachable", return_value=True),
        ):
            chk = _check_reranker_reachable({})
        assert chk["passed"] is True
        assert chk.get("severity") is None
        assert "reachable" in chk["detail"]

    def test_unreachable_warns_but_never_fails(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "SIGNAL_INTENT_BACKEND": "reranker",
                    "SIGNAL_INTENT_RERANK_URL": "http://127.0.0.1:47952",
                },
                clear=False,
            ),
            patch("agentalloy.install.state.rerank_reachable", return_value=False),
        ):
            chk = _check_reranker_reachable({})
        assert chk["passed"] is True  # advisory — never fails the suite
        assert chk["severity"] == "warn"
        assert "cosine floor" in chk["detail"]
        assert "remediation" in chk


# ---------------------------------------------------------------------------
# Check 1: embedding endpoint reachable
# ---------------------------------------------------------------------------


class TestEmbeddingEndpointReachable:
    @patch("agentalloy.install.subcommands.verify.urlopen")
    def test_pass_on_200(self, mock_urlopen: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s  # pyright: ignore[reportUnknownLambdaType]
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp
        result = _check_embedding_endpoint_reachable("http://localhost:11434")
        assert result["passed"] is True

    @patch("agentalloy.install.subcommands.verify.urlopen", side_effect=URLError("refused"))
    def test_fail_on_connection_error(self, mock: MagicMock) -> None:
        result = _check_embedding_endpoint_reachable("http://localhost:11434")
        assert result["passed"] is False
        assert "remediation" in result


# ---------------------------------------------------------------------------
# Check 2: embedding 768-dim
# ---------------------------------------------------------------------------


class TestEmbeddingExpectedDim:
    @patch("agentalloy.install.subcommands.verify.urlopen")
    def test_pass_on_expected_dim(self, mock_urlopen: MagicMock) -> None:
        body = json.dumps({"data": [{"embedding": [0.1] * 768}]}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = body
        mock_resp.__enter__ = lambda s: s  # pyright: ignore[reportUnknownLambdaType]
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp
        result = _check_embedding_expected_dim("http://localhost:11434", "nomic-embed-text-v1.5")
        assert result["passed"] is True

    @patch("agentalloy.install.subcommands.verify.urlopen")
    def test_fail_on_wrong_dim(self, mock_urlopen: MagicMock) -> None:
        body = json.dumps({"data": [{"embedding": [0.1] * 1024}]}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = body
        mock_resp.__enter__ = lambda s: s  # pyright: ignore[reportUnknownLambdaType]
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp
        result = _check_embedding_expected_dim("http://localhost:11434", "wrong-model")
        assert result["passed"] is False
        assert "1024" in result.get("error", "")


# ---------------------------------------------------------------------------
# Container mode: embed checks via /diagnostics/runtime
# ---------------------------------------------------------------------------


class TestEmbeddingViaDiagnostics:
    """Container deployments source embed status from the diagnostics endpoint.

    The diagnostics endpoint emits the readiness block under the JSON key
    ``dependency_readiness`` (see ``DependencyReadiness`` in
    ``agentalloy.api.diagnostics_router``). These tests use the real wire
    name so a future schema change to the production response breaks the
    test instead of silently passing.
    """

    def test_passes_when_diag_reports_embedding_ok(self) -> None:
        diag = {"dependency_readiness": {"embedding_runtime": "ok"}}
        result = _check_embedding_via_diagnostics(diag, "embedding_endpoint_reachable")
        assert result["passed"] is True
        assert "embedding_runtime=ok" in result["detail"]
        assert result["name"] == "embedding_endpoint_reachable"

    def test_fails_when_diag_reports_unavailable(self) -> None:
        diag = {"dependency_readiness": {"embedding_runtime": "unavailable"}}
        result = _check_embedding_via_diagnostics(diag, "embedding_endpoint_returns_expected_dim")
        assert result["passed"] is False
        assert "unavailable" in result["error"]
        assert "logs agentalloy" in result["remediation"]

    def test_accepts_legacy_dep_readiness_key(self) -> None:
        """Older snapshots / cached responses may still carry the legacy key."""
        diag = {"dep_readiness": {"embedding_runtime": "ok"}}
        result = _check_embedding_via_diagnostics(diag, "embedding_endpoint_reachable")
        assert result["passed"] is True

    def test_fails_when_diag_is_none(self) -> None:
        """Service unreachable on the runtime port — surface a useful message."""
        result = _check_embedding_via_diagnostics(None, "embedding_endpoint_reachable")
        assert result["passed"] is False
        assert "/diagnostics/runtime unreachable" in result["error"]

    def test_run_checks_uses_diagnostics_in_container_mode(self) -> None:
        """run_checks routes the two embed checks through diagnostics when deployment=container."""
        diag = {"dependency_readiness": {"embedding_runtime": "ok"}}
        st: dict[str, Any] = {"deployment": "container", "port": 47950}
        with (
            patch("agentalloy.install.subcommands.verify._probe_diagnostics", return_value=diag),
            # The direct-probe checks must NOT be called in container mode.
            patch(
                "agentalloy.install.subcommands.verify._check_embedding_endpoint_reachable"
            ) as direct_reach,
            patch(
                "agentalloy.install.subcommands.verify._check_embedding_expected_dim"
            ) as direct_dim,
            # Stub out the DB / harness / port checks so the test stays focused.
            patch(
                "agentalloy.install.subcommands.verify._check_duckdb_present",
                return_value={"name": "duckdb_present", "passed": True},
            ),
            patch(
                "agentalloy.install.subcommands.verify._check_ladybug_present",
                return_value={"name": "ladybug_present", "passed": True},
            ),
            patch(
                "agentalloy.install.subcommands.verify._check_skill_count",
                return_value={"name": "skill_count_meets_minimum", "passed": True},
            ),
            patch(
                "agentalloy.install.subcommands.verify._check_harness_config_present",
                return_value={"name": "harness_config_present", "passed": True},
            ),
            patch(
                "agentalloy.install.subcommands.verify._check_harness_config_url",
                return_value={"name": "harness_config_url_matches", "passed": True},
            ),
            patch(
                "agentalloy.install.subcommands.verify._check_port_available",
                return_value={"name": "runtime_port_available", "passed": True},
            ),
        ):
            result = run_checks(st)

        direct_reach.assert_not_called()
        direct_dim.assert_not_called()
        check_names = [c["name"] for c in result["checks"]]
        assert "embedding_endpoint_reachable" in check_names
        assert "embedding_endpoint_returns_expected_dim" in check_names
        # Both embed checks should reflect diagnostics-sourced detail.
        embed_checks = [c for c in result["checks"] if c["name"].startswith("embedding_")]
        for c in embed_checks:
            assert "diagnostics/runtime" in (c.get("detail") or "")


# ---------------------------------------------------------------------------
# Check 3: DuckDB present
# ---------------------------------------------------------------------------


class TestDuckDBPresent:
    def test_fail_on_missing_file(self) -> None:
        result = _check_duckdb_present("/nonexistent/path/skills.duck")
        assert result["passed"] is False


# ---------------------------------------------------------------------------
# Check 4: Ladybug present
# ---------------------------------------------------------------------------


class TestLadybugPresent:
    def test_fail_on_missing_dir(self) -> None:
        result = _check_ladybug_present("/nonexistent/path/ladybug")
        assert result["passed"] is False


# ---------------------------------------------------------------------------
# Check 6: harness config present
# ---------------------------------------------------------------------------


class TestHarnessConfigPresent:
    def test_pass_for_manual_harness_with_no_files(self) -> None:
        """Empty harness_files_written is valid for manual harness."""
        st: dict[str, Any] = {"harness_files_written": []}
        result = _check_harness_config_present(st)
        # Empty list means manual harness — user-owned config
        assert result["passed"] is True
        assert "manual" in result.get("detail", "")

    def test_pass_with_sentinel(self, tmp_path: Path) -> None:
        harness_file = tmp_path / "CLAUDE.md"
        harness_file.write_text(
            "# Claude\n<!-- BEGIN agentalloy install -->\nstuff\n<!-- END agentalloy install -->\n"
        )
        st: dict[str, Any] = {
            "harness": "claude-code",
            "harness_files_written": [
                {"path": str(harness_file), "sentinel_begin": "<!-- BEGIN agentalloy install -->"}
            ],
        }
        result = _check_harness_config_present(st)
        assert result["passed"] is True

    def test_fail_when_sentinel_missing(self, tmp_path: Path) -> None:
        harness_file = tmp_path / "CLAUDE.md"
        harness_file.write_text("# Claude\nno sentinel here\n")
        st: dict[str, Any] = {
            "harness_files_written": [
                {"path": str(harness_file), "sentinel_begin": "<!-- BEGIN agentalloy install -->"}
            ],
        }
        result = _check_harness_config_present(st)
        assert result["passed"] is False

    def test_pass_when_manual_harness(self) -> None:
        """Empty harness_files_written with no integration_vector is manual harness."""
        st: dict[str, Any] = {"harness_files_written": []}
        result = _check_harness_config_present(st)
        assert result["passed"] is True
        assert "manual" in result["detail"]

    def test_pass_when_dedicated_file_has_null_sentinel(self, tmp_path: Path) -> None:
        """Dedicated files (we own the whole file) record sentinel_begin=None.

        The check must treat that as "no sentinel to verify" instead of
        crashing on `None not in content`.
        """
        harness_file = tmp_path / ".agentalloy-aider-instructions.md"
        harness_file.write_text("rendered content\n")
        st: dict[str, Any] = {
            "harness_files_written": [{"path": str(harness_file), "sentinel_begin": None}],
        }
        result = _check_harness_config_present(st)
        assert result["passed"] is True


# ---------------------------------------------------------------------------
# Check 7: harness URL matches
# ---------------------------------------------------------------------------


class TestHarnessConfigURL:
    def test_pass_when_url_present(self, tmp_path: Path) -> None:
        f = tmp_path / "CLAUDE.md"
        f.write_text("http://localhost:8000/compose\n")
        st: dict[str, Any] = {
            "port": 8000,
            "harness_files_written": [{"path": str(f)}],
        }
        result = _check_harness_config_url(st)
        assert result["passed"] is True

    def test_pass_when_manual_harness(self) -> None:
        """Empty harness_files_written with no integration_vector is manual harness."""
        st: dict[str, Any] = {"port": 8000, "harness_files_written": []}
        result = _check_harness_config_url(st)
        assert result["passed"] is True
        assert "manual" in result["detail"]

    def test_fail_when_url_missing(self, tmp_path: Path) -> None:
        f = tmp_path / "CLAUDE.md"
        f.write_text("http://localhost:9999/compose\n")
        st: dict[str, Any] = {
            "port": 8000,
            "harness_files_written": [{"path": str(f)}],
        }
        result = _check_harness_config_url(st)
        assert result["passed"] is False


# ---------------------------------------------------------------------------
# Check 8: port available
# ---------------------------------------------------------------------------


class TestPortAvailable:
    def test_unused_port_passes(self) -> None:
        # Port 0 should be available (OS assigns ephemeral)
        result = _check_port_available(19999)
        # Might pass or fail depending on what's running, but should not error
        assert "name" in result
        assert result["name"] == "runtime_port_available"


class TestPortAvailableHealthStatus:
    """`_check_port_available` consults `/health` when the port is bound.

    Regression: previously compared `status == "ok"`, but the service
    returns `"healthy"` / `"degraded"` / `"unavailable"`.
    """

    @patch("agentalloy.install.subcommands.verify.urlopen")
    @patch("agentalloy.install.subcommands.verify.socket.socket")
    def test_healthy_status_passes(self, mock_sock: MagicMock, mock_urlopen: MagicMock) -> None:
        # Mock the TCP connect_ex to return 0 (port in use).
        sock_inst = MagicMock()
        sock_inst.connect_ex.return_value = 0
        sock_inst.__enter__ = lambda s: s  # pyright: ignore[reportUnknownLambdaType]
        sock_inst.__exit__ = MagicMock(return_value=False)
        mock_sock.return_value = sock_inst

        body = json.dumps({"status": "healthy"}).encode()
        resp = MagicMock()
        resp.read.return_value = body
        resp.__enter__ = lambda s: s  # pyright: ignore[reportUnknownLambdaType]
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        result = _check_port_available(47950)
        assert result["passed"] is True

    @patch("agentalloy.install.subcommands.verify.urlopen")
    @patch("agentalloy.install.subcommands.verify.socket.socket")
    def test_degraded_status_passes(self, mock_sock: MagicMock, mock_urlopen: MagicMock) -> None:
        sock_inst = MagicMock()
        sock_inst.connect_ex.return_value = 0
        sock_inst.__enter__ = lambda s: s  # pyright: ignore[reportUnknownLambdaType]
        sock_inst.__exit__ = MagicMock(return_value=False)
        mock_sock.return_value = sock_inst

        body = json.dumps({"status": "degraded"}).encode()
        resp = MagicMock()
        resp.read.return_value = body
        resp.__enter__ = lambda s: s  # pyright: ignore[reportUnknownLambdaType]
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        result = _check_port_available(47950)
        assert result["passed"] is True

    @patch("agentalloy.install.subcommands.verify.urlopen")
    @patch("agentalloy.install.subcommands.verify.socket.socket")
    def test_unavailable_status_fails(self, mock_sock: MagicMock, mock_urlopen: MagicMock) -> None:
        sock_inst = MagicMock()
        sock_inst.connect_ex.return_value = 0
        sock_inst.__enter__ = lambda s: s  # pyright: ignore[reportUnknownLambdaType]
        sock_inst.__exit__ = MagicMock(return_value=False)
        mock_sock.return_value = sock_inst

        body = json.dumps({"status": "unavailable"}).encode()
        resp = MagicMock()
        resp.read.return_value = body
        resp.__enter__ = lambda s: s  # pyright: ignore[reportUnknownLambdaType]
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        result = _check_port_available(47950)
        assert result["passed"] is False
        assert "'unavailable'" in result.get("error", "")


class TestDBChecksWithServiceUp:
    """When `diag` is provided, the three DB checks must NOT touch the
    DB files — the service holds the locks. They derive pass/fail from
    the diagnostics response instead.
    """

    def _diag(self, *, runtime: str, telemetry: str, skills: int) -> dict[str, Any]:
        return {
            "dependency_readiness": {
                "runtime_store": runtime,
                "telemetry_store": telemetry,
                "embedding_runtime": "ok",
                "runtime_cache": "ok",
            },
            "store_state": [{"skill_id": f"s{i}"} for i in range(skills)],
        }

    def test_duckdb_passes_when_telemetry_ok(self) -> None:
        diag = self._diag(runtime="ok", telemetry="ok", skills=29)
        result = _check_duckdb_present("/path/that/does/not/exist.duck", diag=diag)
        assert result["passed"] is True
        assert "/diagnostics/runtime" in result["detail"]

    def test_duckdb_fails_when_telemetry_unavailable(self) -> None:
        diag = self._diag(runtime="ok", telemetry="unavailable", skills=29)
        result = _check_duckdb_present("/path/that/does/not/exist.duck", diag=diag)
        assert result["passed"] is False
        assert "telemetry_store" in result["error"]

    def test_ladybug_passes_when_runtime_ok(self) -> None:
        diag = self._diag(runtime="ok", telemetry="ok", skills=29)
        result = _check_ladybug_present("/path/that/does/not/exist", diag=diag)
        assert result["passed"] is True
        assert "29 active skills" in result["detail"]

    def test_ladybug_fails_when_runtime_unavailable(self) -> None:
        diag = self._diag(runtime="unavailable", telemetry="ok", skills=29)
        result = _check_ladybug_present("/path/that/does/not/exist", diag=diag)
        assert result["passed"] is False
        assert "runtime_store" in result["error"]

    def test_skill_count_passes_when_at_minimum(self) -> None:
        diag = self._diag(runtime="ok", telemetry="ok", skills=MIN_SKILL_COUNT)
        result = _check_skill_count("/unused", diag=diag)
        assert result["passed"] is True

    def test_skill_count_fails_when_below_minimum(self) -> None:
        diag = self._diag(runtime="ok", telemetry="ok", skills=MIN_SKILL_COUNT - 1)
        result = _check_skill_count("/unused", diag=diag)
        assert result["passed"] is False


# ---------------------------------------------------------------------------
# Container fallback: diag is None, is_container=True
# ---------------------------------------------------------------------------


class TestContainerFallback:
    """When diag is None and is_container=True, the DB checks must
    skip direct file access and return a clear service-unreachable error.

    This covers the container deployment scenario where _probe_diagnostics()
    fails (service unreachable from the host) and the fallback would
    previously try to open DB files that only exist inside the container.
    """

    def test_duckdb_fallback_skips_file_access(self) -> None:
        result = _check_duckdb_present("/nonexistent.duck", diag=None, is_container=True)
        assert result["passed"] is False
        assert "Service not reachable" in result["error"]
        assert "diagnostics endpoint" in result["error"]
        assert "container logs" in result["remediation"]

    def test_ladybug_fallback_skips_file_access(self) -> None:
        result = _check_ladybug_present("/nonexistent/ladybug", diag=None, is_container=True)
        assert result["passed"] is False
        assert "Service not reachable" in result["error"]
        assert "Kuzu DB" in result["error"]
        assert "container logs" in result["remediation"]

    def test_skill_count_fallback_skips_file_access(self) -> None:
        result = _check_skill_count("/nonexistent/ladybug", diag=None, is_container=True)
        assert result["passed"] is False
        assert "Service not reachable" in result["error"]
        assert "count skills" in result["error"]
        assert "container logs" in result["remediation"]

    def test_non_container_fallback_still_tries_file(self, tmp_path: Path) -> None:
        """Non-container mode with diag=None should still try direct file access."""
        result = _check_duckdb_present("/nonexistent/path.duck", diag=None, is_container=False)
        assert result["passed"] is False
        assert "/nonexistent/path.duck not found" in result["error"]


# ---------------------------------------------------------------------------
# Full run_checks
# ---------------------------------------------------------------------------


class TestRunChecks:
    @patch("agentalloy.install.subcommands.verify.urlopen", side_effect=URLError("refused"))
    def test_output_schema(self, mock_urlopen: MagicMock, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("")
        st: dict[str, Any] = {
            "port": 8000,
            "harness_files_written": [],
        }
        result = run_checks(st, root=tmp_path)
        assert result["schema_version"] == SCHEMA_VERSION
        assert "all_checks_passed" in result
        assert "checks" in result
        assert len(result["checks"]) == 9

    @patch("agentalloy.install.subcommands.verify.urlopen", side_effect=URLError("refused"))
    def test_all_checks_have_name_and_passed(self, mock_urlopen: MagicMock, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("")
        st: dict[str, Any] = {"port": 8000, "harness_files_written": []}
        result = run_checks(st, root=tmp_path)
        for check in result["checks"]:
            assert "name" in check
            assert "passed" in check
