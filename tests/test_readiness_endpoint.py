"""Tests for ReadinessResponse model and ReadinessChecker class."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from agentalloy.api.health_router import (
    ReadinessChecker,
    ReadinessResponse,
)
from agentalloy.api.health_router import (
    router as readiness_router,
)

# ---------------------------------------------------------------------------
# UT-1: ReadinessResponse model validation
# ---------------------------------------------------------------------------


class TestReadinessResponseModel:
    """[UT-1] ReadinessResponse accepts valid status values and optional progress."""

    def test_valid_ready_status(self):
        resp = ReadinessResponse(status="ready")
        assert resp.status == "ready"
        assert resp.progress is None

    def test_valid_warming_up_status(self):
        resp = ReadinessResponse(status="warming_up")
        assert resp.status == "warming_up"

    def test_valid_error_status(self):
        resp = ReadinessResponse(status="error")
        assert resp.status == "error"

    def test_valid_status_with_progress(self):
        progress = {"packs_ingested": 5, "packs_total": 20}
        resp = ReadinessResponse(status="warming_up", progress=progress)
        assert resp.status == "warming_up"
        assert resp.progress == progress

    def test_invalid_status_rejected(self):
        with pytest.raises(ValidationError):
            ReadinessResponse(status="unknown")

    def test_invalid_status_rejected_uppercase(self):
        with pytest.raises(ValidationError):
            ReadinessResponse(status="READY")

    def test_invalid_status_rejected_typo(self):
        with pytest.raises(ValidationError):
            ReadinessResponse(status="warmin_up")


# ---------------------------------------------------------------------------
# UT-2 to UT-7: ReadinessChecker file-based state machine
# ---------------------------------------------------------------------------


class TestReadinessChecker:
    """[UT-2]..[UT-7] ReadinessChecker maps file states to status values."""

    def _make_checker(self, tmp_path: Path) -> ReadinessChecker:
        """Create a ReadinessChecker pointing at tmp_path."""
        return ReadinessChecker(app_dir=tmp_path)

    def test_2_ready_when_bootstrap_complete_exists(self, tmp_path: Path) -> None:
        """[UT-2] Returns 'ready' when .bootstrap-complete exists."""
        checker = self._make_checker(tmp_path)
        # Create the complete file
        (tmp_path / ".bootstrap-complete").touch()

        result = checker.check()
        assert result.status == "ready"

    def test_3_warming_up_when_lock_not_stale(self, tmp_path: Path) -> None:
        """[UT-3] Returns 'warming_up' when .bootstrap-lock exists and is not stale."""
        checker = self._make_checker(tmp_path)
        # Create lock file with recent timestamp
        lock_file = tmp_path / ".bootstrap-lock"
        lock_file.write_text(time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()))
        # Do NOT create complete file

        result = checker.check()
        assert result.status == "warming_up"

    def test_4_error_with_stale_lock(self, tmp_path: Path) -> None:
        """[UT-4] Returns 'error' with stale_lock when lock > 2h old."""
        checker = self._make_checker(tmp_path)
        # Create lock file with old timestamp (> 2 hours ago)
        old_time = time.strftime(
            "%Y-%m-%dT%H:%M:%S%z",
            time.localtime(time.time() - (2 * 3600 + 60)),  # 2h + 1m ago
        )
        (tmp_path / ".bootstrap-lock").write_text(old_time)
        # Do NOT create complete file

        result = checker.check()
        assert result.status == "error"
        assert result.progress is not None
        assert result.progress.get("error") == "stale_lock"

    def test_5_ready_when_neither_file_exists(self, tmp_path: Path) -> None:
        """[UT-5] Returns 'ready' when neither lock nor complete exists."""
        checker = self._make_checker(tmp_path)
        # No files at all

        result = checker.check()
        assert result.status == "ready"

    def test_6_warming_up_with_partial_progress(self, tmp_path: Path) -> None:
        """[UT-6] Returns 'warming_up' with partial progress when progress file missing."""
        checker = self._make_checker(tmp_path)
        # Create lock file (not stale) but NO progress file
        lock_file = tmp_path / ".bootstrap-lock"
        lock_file.write_text(time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()))

        result = checker.check()
        assert result.status == "warming_up"
        assert result.progress is not None
        # Progress should indicate no data available
        assert result.progress.get("packs_ingested") == 0
        assert result.progress.get("packs_total") == 0

    def test_7_handles_invalid_json_in_progress(self, tmp_path: Path) -> None:
        """[UT-7] Handles invalid JSON in progress file gracefully."""
        checker = self._make_checker(tmp_path)
        # Create lock file (not stale) and invalid progress file
        lock_file = tmp_path / ".bootstrap-lock"
        lock_file.write_text(time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()))
        (tmp_path / ".bootstrap-progress").write_text("not valid json {{{")

        result = checker.check()
        assert result.status == "warming_up"
        assert result.progress is not None
        # Should fall back to zero counts like missing progress
        assert result.progress.get("packs_ingested") == 0
        assert result.progress.get("packs_total") == 0

    def test_complete_file_supersedes_lock(self, tmp_path: Path) -> None:
        """Complete file takes priority over lock file."""
        checker = self._make_checker(tmp_path)
        (tmp_path / ".bootstrap-lock").write_text("stale")
        (tmp_path / ".bootstrap-complete").touch()

        result = checker.check()
        assert result.status == "ready"

    def test_progress_file_parsed_correctly(self, tmp_path: Path) -> None:
        """Progress file JSON is parsed and returned correctly."""
        checker = self._make_checker(tmp_path)
        lock_file = tmp_path / ".bootstrap-lock"
        lock_file.write_text(time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()))
        progress_data = {
            "packs_ingested": 10,
            "packs_total": 20,
            "embeddings_done": 500,
            "embeddings_total": 2949,
            "current_pack": "python",
            "started_at": "2025-01-01T00:00:00Z",
            "updated_at": "2025-01-01T00:05:00Z",
        }
        (tmp_path / ".bootstrap-progress").write_text(json.dumps(progress_data))

        result = checker.check()
        assert result.status == "warming_up"
        assert result.progress["packs_ingested"] == 10
        assert result.progress["packs_total"] == 20
        assert result.progress["embeddings_done"] == 500
        assert result.progress["embeddings_total"] == 2949
        assert result.progress["current_pack"] == "python"
        assert result.progress["started_at"] == "2025-01-01T00:00:00Z"


class TestReadinessEndpoint:
    """Test GET /readiness endpoint integration."""

    @pytest.fixture
    def app_with_readiness(self, tmp_path: Path) -> FastAPI:
        """Create a FastAPI app with the readiness router mounted."""
        app = FastAPI()
        app.include_router(readiness_router)
        return app

    def test_readiness_endpoint_200(self, app_with_readiness: FastAPI) -> None:
        """GET /readiness returns 200 OK."""
        with TestClient(app_with_readiness) as c:
            resp = c.get("/readiness")
        assert resp.status_code == 200

    def test_readiness_default_status_ready(self, app_with_readiness: FastAPI) -> None:
        """Without a ReadinessChecker in app.state, returns ready."""
        with TestClient(app_with_readiness) as c:
            resp = c.get("/readiness")
        body = resp.json()
        assert body["status"] == "ready"

    def test_readiness_with_checker(self, app_with_readiness: FastAPI, tmp_path: Path) -> None:
        """With a ReadinessChecker in app.state, returns its result."""
        checker = ReadinessChecker(app_dir=tmp_path)
        (tmp_path / ".bootstrap-complete").touch()
        app_with_readiness.state.readiness_checker = checker

        with TestClient(app_with_readiness) as c:
            resp = c.get("/readiness")
        body = resp.json()
        assert body["status"] == "ready"
