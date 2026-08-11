"""Tests for the ship watcher and merged_status gate."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agentalloy.signals.predicates import (
    PredicateContext,
    PredicateResult,
    eval_merged_status,
)
from agentalloy.storage.state_store import DuckDBStateStore, bind_process_store
from agentalloy.watcher.lifecycle import LifecycleManager
from agentalloy.watcher.pr_opener import PRManager
from agentalloy.watcher.reset_ask import ResetPrompter
from agentalloy.watcher.telemetry import TelemetryStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store(tmp_path: Path) -> DuckDBStateStore:
    """Create a DuckDB store with ship-phase tables."""
    db = tmp_path / "test_ship.db"
    store = DuckDBStateStore(db)
    store.open()
    store.migrate()
    return store


def _ctx(tmp_path: Path, store: DuckDBStateStore, **kwargs) -> PredicateContext:
    defaults = {"project_root": tmp_path, "current_phase": "ship"}
    defaults["store"] = store
    defaults.update(kwargs)
    return PredicateContext(**defaults)


# ---------------------------------------------------------------------------
# eval_merged_status
# ---------------------------------------------------------------------------


class TestEvalMergedStatus:
    """Tests for the merged_status predicate."""

    def test_met_when_merged(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.upsert_pr_lifecycle("add-telemetry", merged_at="2026-08-10T12:00:00Z")

        ctx = _ctx(tmp_path, store=store)
        result = eval_merged_status({"task_slug": "add-telemetry"}, ctx)
        assert result == PredicateResult.MET
        store.close()

    def test_not_met_when_not_merged(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.upsert_pr_lifecycle("add-telemetry", pr_url="https://github.com/foo/bar/pull/42")

        ctx = _ctx(tmp_path, store=store)
        result = eval_merged_status({"task_slug": "add-telemetry"}, ctx)
        assert result == PredicateResult.NOT_MET
        store.close()

    def test_not_met_when_empty_merged_at(self, tmp_path: Path) -> None:
        """merged_at is None (explicitly stored as empty) → NOT_MET."""
        store = _make_store(tmp_path)
        # Insert row, then update merged_at to NULL
        store.upsert_pr_lifecycle("add-telemetry", merged_at="2026-08-10T12:00:00Z")
        repo, sid = store._repo(), store._sid()
        store.conn.execute(
            "UPDATE pr_lifecycle SET merged_at=NULL WHERE repo=? AND stream_id=? AND task_slug=?",
            (repo, sid, "add-telemetry"),
        )

        ctx = _ctx(tmp_path, store=store)
        result = eval_merged_status({"task_slug": "add-telemetry"}, ctx)
        assert result == PredicateResult.NOT_MET
        store.close()

    def test_unknown_when_no_lifecycle(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)

        ctx = _ctx(tmp_path, store=store)
        result = eval_merged_status({"task_slug": "nonexistent"}, ctx)
        assert result == PredicateResult.UNKNOWN
        store.close()

    def test_unknown_when_no_store(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path, store=None)
        result = eval_merged_status({"task_slug": "add-telemetry"}, ctx)
        assert result == PredicateResult.UNKNOWN

    def test_unknown_when_no_slug_and_no_phase(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)

        ctx = _ctx(tmp_path, store=store, current_phase=None)
        result = eval_merged_status({}, ctx)
        assert result == PredicateResult.UNKNOWN
        store.close()

    def test_resolves_slug_from_phase(self, tmp_path: Path) -> None:
        """When task_slug is not provided, resolves from context.phase.

        resolve_active_slug queries the process-bound store for phase contracts
        and reads the work-item cursor. We bind the test's store as the process
        store, insert a contract, and set the cursor.
        """
        store = _make_store(tmp_path)
        store.upsert_pr_lifecycle(
            "add-telemetry",
            merged_at="2026-08-10T12:00:00Z",
        )

        # Bind this store as the process-wide store so resolve_active_slug can
        # query it (the conftest _bound_state_store binds a different store).
        bind_process_store(store)
        try:
            # Insert a ship-phase contract pointing to "add-telemetry"
            store.put_contract(
                "add-telemetry",
                phase="ship",
                slug="add-telemetry",
                domain_tags=["ship"],
                status="active",
            )

            ctx = _ctx(tmp_path, store=store)
            # No task_slug in args — predicate resolves from phase
            result = eval_merged_status({"phase": "ship"}, ctx)
            assert result == PredicateResult.MET, f"result={result}"
        finally:
            bind_process_store(None)
        store.close()


# ---------------------------------------------------------------------------
# LifecycleManager
# ---------------------------------------------------------------------------


class TestLifecycleManager:
    """Tests for PR lifecycle CRUD."""

    def test_start_watching(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.upsert_pr_lifecycle("add-telemetry")

        lm = LifecycleManager(store=store, task_slug="add-telemetry")
        lm.start_watching()

        info = store.get_pr_lifecycle("add-telemetry")
        assert info is not None
        assert info["watcher_started"] is not None
        store.close()

    def test_set_merged(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.upsert_pr_lifecycle("add-telemetry")

        lm = LifecycleManager(store=store, task_slug="add-telemetry")
        lm.set_merged()

        assert lm.is_merged() is True
        info = store.get_pr_lifecycle("add-telemetry")
        assert info is not None
        assert info["merged_at"] is not None
        store.close()

    def test_increment_ci_failures(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.upsert_pr_lifecycle("add-telemetry")

        lm = LifecycleManager(store=store, task_slug="add-telemetry")
        assert lm.get_ci_failures() == 0

        new_count = lm.increment_ci_failures()
        assert new_count == 1

        new_count = lm.increment_ci_failures()
        assert new_count == 2

        info = store.get_pr_lifecycle("add-telemetry")
        assert info is not None
        assert info["ci_failures"] == 2
        store.close()

    def test_update_pr_url(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.upsert_pr_lifecycle("add-telemetry")

        lm = LifecycleManager(store=store, task_slug="add-telemetry")
        lm.update_pr_url("https://github.com/foo/bar/pull/42")

        assert lm.get_pr_url() == "https://github.com/foo/bar/pull/42"
        store.close()

    def test_get_status(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.upsert_pr_lifecycle("add-telemetry")

        lm = LifecycleManager(store=store, task_slug="add-telemetry")
        lm.set_merged()

        info = lm.get_status()
        assert info is not None
        assert info["merged_at"] is not None
        store.close()

    def test_no_store_returns_defaults(self, tmp_path: Path) -> None:
        """When no store is available, methods return safely."""
        lm = LifecycleManager(store=None, task_slug="add-telemetry")
        assert lm.is_merged() is False
        assert lm.get_ci_failures() == 0
        assert lm.get_pr_url() is None
        assert lm.get_auto_merge() is False


# ---------------------------------------------------------------------------
# TelemetryStore
# ---------------------------------------------------------------------------


class TestTelemetryStore:
    """Tests for CI telemetry recording."""

    def test_record_and_list_ci_check(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.close()

        ts = TelemetryStore()
        ts.record_ci_check(
            pr_url="https://github.com/foo/bar/pull/42",
            check_name="ci/github-actions/Build",
            check_status="completed",
            check_conclusion="success",
        )

        results = ts.list_ci_telemetry("https://github.com/foo/bar/pull/42")
        assert len(results) == 1
        assert results[0]["check_name"] == "ci/github-actions/Build"
        assert results[0]["check_status"] == "completed"
        store.close()


# ---------------------------------------------------------------------------
# PRManager
# ---------------------------------------------------------------------------


class TestPRManager:
    """Tests for PR creation via VCS forge."""

    def test_detect_gh(self, tmp_path: Path) -> None:
        """When gh is on PATH, detects it."""

        pm = PRManager(store=None, lifecycle=MagicMock(), project_root=tmp_path)
        with patch("shutil.which", return_value="/usr/bin/gh"):
            assert pm.vcs_type == "gh"

    def test_detect_glab(self, tmp_path: Path) -> None:
        """When glab is on PATH (no gh), detects it."""

        pm = PRManager(store=None, lifecycle=MagicMock(), project_root=tmp_path)
        with patch("shutil.which", side_effect=lambda cmd: "glab" if cmd == "glab" else None):
            assert pm.vcs_type == "glab"

    def test_detect_none(self, tmp_path: Path) -> None:
        """When neither is on PATH, returns None."""

        pm = PRManager(store=None, lifecycle=MagicMock(), project_root=tmp_path)
        with patch("shutil.which", return_value=None):
            assert pm.vcs_type is None


# ---------------------------------------------------------------------------
# ResetPrompter
# ---------------------------------------------------------------------------


class TestResetPrompter:
    """Tests for post-merge reset prompt."""

    def test_ask_and_reset_writes_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Writes a reset marker file."""

        store = _make_store(tmp_path)

        lm = LifecycleManager(store=store, task_slug="add-telemetry")
        lm.set_merged()

        rp = ResetPrompter(store=store, lifecycle=lm)
        # ResetPrompter writes to CWD-relative .agentalloy, so chdir to tmp_path
        monkeypatch.chdir(tmp_path)
        rp.ask_and_reset("add-telemetry")

        # Check marker file was written
        marker_files = list((tmp_path / ".agentalloy").glob("reset-to-intake-*.pending"))
        assert len(marker_files) == 1
        content = marker_files[0].read_text()
        assert "add-telemetry" in content
        assert "reset to intake" in content
        store.close()
