# ruff: noqa: I001, PLC0415 -- testing private module members intentionally
"""Tests for contract 07b-cli-surface-completion.

Covers:
  TA5  - service down: non-zero exit, nothing written (contracts archive, resume)
  TA6  - .agentalloy/ retains only config and claude-code-env.sh after uninstall
  TA7  - archive via CLI
  TA11 - resume reconstructs a cold session in one command
  TE3  - uninstall sweeps all recorded repos
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agentalloy.api.state_client import StateClient, StateClientError


# ---------------------------------------------------------------------------
# TA5 — service down: non-zero exit, nothing written
# ---------------------------------------------------------------------------


class TestTA5ServiceDown:
    """Service down means a non-zero exit naming the service — never a silent
    local write or stale output."""

    def test_contracts_archive_exits_nonzero_when_service_down(self, capsys) -> None:
        from agentalloy.install.subcommands.contracts import _run_archive

        args = argparse.Namespace(phase=None, slug=None, dry_run=False)

        exit_code: list[int] = []

        def _fake_exit(code: int) -> None:
            exit_code.append(code)
            raise SystemExit(code)

        with (
            patch.object(StateClient, "is_running", return_value=False),
            patch("sys.exit", side_effect=_fake_exit),
        ):
            with pytest.raises(SystemExit):
                _run_archive(args)
        assert exit_code == [1]
        captured = capsys.readouterr()
        assert "service" in captured.err.lower() or "not running" in captured.err.lower()

    def test_resume_exits_nonzero_when_service_down(self, capsys) -> None:
        from agentalloy.install.subcommands.resume import _run

        args = argparse.Namespace(json=False)

        with patch.object(StateClient, "is_running", return_value=False):
            result = _run(args)
        assert result == 1
        captured = capsys.readouterr()
        assert "service" in captured.err.lower() or "not running" in captured.err.lower()

    def test_statusline_shows_degraded_when_service_down(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agentalloy.install.subcommands.statusline import render_statusline

        # Create a wired repo (config file marks it as wired)
        aa_dir = tmp_path / ".agentalloy"
        aa_dir.mkdir(parents=True)
        (aa_dir / "config").write_text("harness: claude-code\n")

        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
        monkeypatch.delenv("AGENTALLOY_RELEASE_CHECK", raising=False)

        with patch.object(StateClient, "is_running", return_value=False):
            line = render_statusline(tmp_path)
        assert "[degraded]" in line


# ---------------------------------------------------------------------------
# TA6 — .agentalloy/ retains only config and claude-code-env.sh
# ---------------------------------------------------------------------------


class TestTA6RetainsConfig:
    """After uninstall, .agentalloy/ contains only config and claude-code-env.sh
    (the wired-repo marker and the per-repo env carrier)."""

    def test_unwire_repo_local_preserves_config(self, tmp_path: Path) -> None:
        from agentalloy.install.subcommands.uninstall import _unwire_repo_local

        repo = tmp_path / "repo"
        repo.mkdir()
        aa = repo / ".agentalloy"
        aa.mkdir()
        (aa / "config").write_text("harness: claude-code\n")
        # A legacy phase file, from a repo that never took the upgrade path. The
        # store owns phase now, so unwire must clear this rather than leave it —
        # `_import_phase_file` would otherwise resurrect the row on a re-add.
        (aa / "phase").write_text("phase: build\n")
        (aa / "upstream").write_text("origin/main\n")
        (aa / "README.md").write_text("test\n")
        (aa / "claude-code-env.sh").write_text("export FOO=bar\n")

        _unwire_repo_local(repo, set(), warnings=[], remove_lifecycle=True)

        # config and claude-code-env.sh should remain
        assert (aa / "config").exists(), "config should be preserved"
        assert (aa / "claude-code-env.sh").exists(), "claude-code-env.sh should be preserved"
        assert not (aa / "upstream").exists(), "upstream should be removed"
        assert not (aa / "README.md").exists(), "README.md should be removed"
        assert not (aa / "phase").exists(), "a legacy phase file should be removed"

    def test_unwire_repo_local_does_not_remove_contracts_dir(self, tmp_path: Path) -> None:
        from agentalloy.install.subcommands.uninstall import _unwire_repo_local

        repo = tmp_path / "repo"
        repo.mkdir()
        aa = repo / ".agentalloy"
        aa.mkdir()
        (aa / "config").write_text("harness: claude-code\n")
        contracts_dir = aa / "contracts"
        contracts_dir.mkdir()
        (contracts_dir / "active").mkdir()
        (contracts_dir / "active" / "test.md").write_text("# test contract\n")

        _unwire_repo_local(repo, set(), warnings=[], remove_lifecycle=True)

        # contracts directory should be preserved
        assert (contracts_dir / "active" / "test.md").exists()


# ---------------------------------------------------------------------------
# TA7 — archive via CLI
# ---------------------------------------------------------------------------


class TestTA7ArchiveViaCLI:
    """contracts archive flips contract status through StateClient over HTTP."""

    def test_archive_flips_status_via_state_client(self) -> None:
        from agentalloy.install.subcommands.contracts import _run_archive

        mock_client = MagicMock(spec=StateClient)
        mock_client.is_running.return_value = True
        mock_client.list_contracts.return_value = [
            {"contract_id": "test-001", "slug": "test-contract", "status": "active"},
            {"contract_id": "test-002", "slug": "another-contract", "status": "active"},
        ]
        mock_client.archive_contract.return_value = {"success": True}

        args = argparse.Namespace(phase=None, slug=None, dry_run=False)

        with patch(
            "agentalloy.install.subcommands.contracts.StateClient",
            return_value=mock_client,
        ):
            result = _run_archive(args)

        assert result == 0
        assert mock_client.archive_contract.call_count == 2
        mock_client.archive_contract.assert_any_call("test-001")
        mock_client.archive_contract.assert_any_call("test-002")

    def test_archive_dry_run_does_not_flip(self) -> None:
        from agentalloy.install.subcommands.contracts import _run_archive

        mock_client = MagicMock(spec=StateClient)
        mock_client.is_running.return_value = True
        mock_client.list_contracts.return_value = [
            {"contract_id": "test-001", "slug": "test", "status": "active"},
        ]

        args = argparse.Namespace(phase=None, slug=None, dry_run=True)

        with patch(
            "agentalloy.install.subcommands.contracts.StateClient",
            return_value=mock_client,
        ):
            result = _run_archive(args)

        assert result == 0
        mock_client.archive_contract.assert_not_called()

    def test_archive_filters_by_phase(self) -> None:
        from agentalloy.install.subcommands.contracts import _run_archive

        mock_client = MagicMock(spec=StateClient)
        mock_client.is_running.return_value = True
        mock_client.list_contracts.return_value = [
            {"contract_id": "build-001", "slug": "x", "status": "active"},
        ]

        args = argparse.Namespace(phase="build", slug=None, dry_run=False)

        with patch(
            "agentalloy.install.subcommands.contracts.StateClient",
            return_value=mock_client,
        ):
            _run_archive(args)

        mock_client.list_contracts.assert_called_once_with(
            phase="build", slug=None, status="active"
        )

    def test_archive_handles_partial_failure(self) -> None:
        from agentalloy.install.subcommands.contracts import _run_archive

        mock_client = MagicMock(spec=StateClient)
        mock_client.is_running.return_value = True
        mock_client.list_contracts.return_value = [
            {"contract_id": "ok-001", "slug": "x", "status": "active"},
            {"contract_id": "fail-001", "slug": "y", "status": "active"},
        ]
        mock_client.archive_contract.side_effect = [
            {"success": True},
            StateClientError("connection reset"),
        ]

        args = argparse.Namespace(phase=None, slug=None, dry_run=False)

        with patch(
            "agentalloy.install.subcommands.contracts.StateClient",
            return_value=mock_client,
        ):
            result = _run_archive(args)

        assert result == 1  # non-zero due to error


# ---------------------------------------------------------------------------
# TA11 — resume reconstructs a cold session in one command
# ---------------------------------------------------------------------------


class TestTA11ResumeReconstructsSession:
    """resume prints phase, cursor'd work-item, domain_tags, scope, owed
    artifacts, and governing decisions from one GET /state/resume."""

    def test_resume_renders_full_payload(self, capsys) -> None:
        from agentalloy.install.subcommands.resume import _run

        mock_client = MagicMock(spec=StateClient)
        mock_client.is_running.return_value = True
        mock_client.get_resume.return_value = {
            "phase": "build",
            "cursor_contract": {
                "contract_id": "07b-cli-surface",
                "slug": "07b-cli-surface-completion",
                "domain_tags": ["cli", "state-management"],
                "scope_touches": [
                    "src/agentalloy/install/subcommands/resume.py",
                ],
                "scope_avoids": ["src/agentalloy/storage/state_store.py"],
                "body": "# Test contract body",
            },
            "owed_artifacts": [
                "src/agentalloy/install/subcommands/resume.py",
                "tests/install/test_cli_surface_07b.py",
            ],
            "governing_decisions": [
                "StateClient is the only path for CLI state reads",
            ],
        }

        args = argparse.Namespace(json=False)

        with patch(
            "agentalloy.install.subcommands.resume.StateClient",
            return_value=mock_client,
        ):
            result = _run(args)

        assert result == 0
        captured = capsys.readouterr()
        assert "build" in captured.out
        assert "07b-cli-surface" in captured.out

    def test_resume_handles_empty_payload(self, capsys) -> None:
        from agentalloy.install.subcommands.resume import _run

        mock_client = MagicMock(spec=StateClient)
        mock_client.is_running.return_value = True
        mock_client.get_resume.return_value = {
            "phase": None,
            "cursor_contract": None,
            "owed_artifacts": [],
            "governing_decisions": [],
        }

        args = argparse.Namespace(json=False)

        with patch(
            "agentalloy.install.subcommands.resume.StateClient",
            return_value=mock_client,
        ):
            result = _run(args)

        assert result == 0

    def test_resume_json_output(self, capsys) -> None:
        from agentalloy.install.subcommands.resume import _run

        mock_client = MagicMock(spec=StateClient)
        mock_client.is_running.return_value = True
        mock_client.get_resume.return_value = {
            "phase": "spec",
            "cursor_contract": {"contract_id": "test", "slug": "test"},
            "owed_artifacts": [],
            "governing_decisions": [],
        }

        args = argparse.Namespace(json=True)

        with patch(
            "agentalloy.install.subcommands.resume.StateClient",
            return_value=mock_client,
        ):
            result = _run(args)

        assert result == 0
        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["phase"] == "spec"


# ---------------------------------------------------------------------------
# TE3 — uninstall sweeps all recorded repos
# ---------------------------------------------------------------------------


class TestTE3UninstallSweepsAllRepos:
    """uninstall drops store rows for every recorded repo, not only cwd."""

    def test_unwire_repo_local_uses_the_bound_store(self, tmp_path: Path) -> None:
        """The bound handle wins, and is never closed — it is not ours to close."""
        from agentalloy.install.subcommands.uninstall import _unwire_repo_local

        repo = tmp_path / "repo"
        repo.mkdir()
        aa = repo / ".agentalloy"
        aa.mkdir()
        (aa / "config").write_text("harness: claude-code\n")

        mock_store = MagicMock()
        mock_store.delete_repo_rows.return_value = 5

        with patch(
            "agentalloy.storage.state_store.process_store",
            return_value=mock_store,
        ):
            _unwire_repo_local(repo, set(), warnings=[], remove_lifecycle=True)

        mock_store.delete_repo_rows.assert_called_once_with(repo.name)
        mock_store.close.assert_not_called()

    def test_unwire_repo_local_opens_directly_when_nothing_is_listening(
        self, tmp_path: Path
    ) -> None:
        from agentalloy.install.subcommands.uninstall import _unwire_repo_local

        repo = tmp_path / "repo"
        repo.mkdir()
        aa = repo / ".agentalloy"
        aa.mkdir()
        (aa / "config").write_text("harness: claude-code\n")

        mock_store = MagicMock()
        mock_store.delete_repo_rows.return_value = 5

        mock_settings = MagicMock()
        mock_settings.duckdb_path = str(tmp_path / "agentalloy.duck")

        with (
            patch("agentalloy.storage.state_store.process_store", return_value=None),
            patch(
                "agentalloy.install.subcommands.uninstall._state_service_running",
                return_value=False,
            ),
            patch("agentalloy.config.get_settings", return_value=mock_settings),
            patch(
                "agentalloy.storage.state_store.open_state_store",
                return_value=mock_store,
            ),
        ):
            _unwire_repo_local(repo, set(), warnings=[], remove_lifecycle=True)

        mock_store.delete_repo_rows.assert_called_once_with(repo.name)
        mock_store.close.assert_called_once()

    def test_unwire_repo_local_drops_rows_via_service_when_live(self, tmp_path: Path) -> None:
        """The deadlock guard: a running service means no second writer handle.

        Out of process with the service up, opening ``state.duck`` read-write
        blocks on DuckDB's lock — so this goes over HTTP to the service's own
        handle instead, rather than skipping the rows entirely.
        """
        from agentalloy.install.subcommands.uninstall import _unwire_repo_local

        repo = tmp_path / "repo"
        repo.mkdir()
        aa = repo / ".agentalloy"
        aa.mkdir()
        (aa / "config").write_text("harness: claude-code\n")

        warnings_list: list[str] = []

        with (
            patch("agentalloy.storage.state_store.process_store", return_value=None),
            patch(
                "agentalloy.install.subcommands.uninstall._state_service_running",
                return_value=True,
            ),
            patch("agentalloy.storage.state_store.open_state_store") as mock_open,
            patch("agentalloy.api.state_client.StateClient.delete_repo_rows", return_value=5),
        ):
            _, files_removed = _unwire_repo_local(repo, set(), warnings_list, remove_lifecycle=True)

        mock_open.assert_not_called()
        assert warnings_list == []
        store_actions = [f for f in files_removed if f.get("action") == "dropped_store_rows"]
        assert store_actions == [
            {"repo": str(repo), "action": "dropped_store_rows", "deleted_rows": 5}
        ]

    def test_unwire_repo_local_warns_when_service_delete_fails(self, tmp_path: Path) -> None:
        from agentalloy.api.state_client import StateClientError
        from agentalloy.install.subcommands.uninstall import _unwire_repo_local

        repo = tmp_path / "repo"
        repo.mkdir()
        aa = repo / ".agentalloy"
        aa.mkdir()
        (aa / "config").write_text("harness: claude-code\n")

        warnings_list: list[str] = []

        with (
            patch("agentalloy.storage.state_store.process_store", return_value=None),
            patch(
                "agentalloy.install.subcommands.uninstall._state_service_running",
                return_value=True,
            ),
            patch(
                "agentalloy.api.state_client.StateClient.delete_repo_rows",
                side_effect=StateClientError("boom"),
            ),
        ):
            _unwire_repo_local(repo, set(), warnings_list, remove_lifecycle=True)

        assert any("Failed to drop store rows" in w for w in warnings_list)

    def test_unwire_repo_local_records_deleted_rows(self, tmp_path: Path) -> None:
        from agentalloy.install.subcommands.uninstall import _unwire_repo_local

        repo = tmp_path / "repo"
        repo.mkdir()
        aa = repo / ".agentalloy"
        aa.mkdir()
        (aa / "config").write_text("harness: claude-code\n")

        mock_store = MagicMock()
        mock_store.delete_repo_rows.return_value = 3

        with patch(
            "agentalloy.storage.state_store.process_store",
            return_value=mock_store,
        ):
            _, files_removed = _unwire_repo_local(repo, set(), warnings=[], remove_lifecycle=True)

        store_actions = [f for f in files_removed if f.get("action") == "dropped_store_rows"]
        assert len(store_actions) == 1
        assert store_actions[0]["deleted_rows"] == 3

    def test_unwire_repo_local_warns_on_store_error(self, tmp_path: Path) -> None:
        from agentalloy.install.subcommands.uninstall import _unwire_repo_local

        repo = tmp_path / "repo"
        repo.mkdir()
        aa = repo / ".agentalloy"
        aa.mkdir()
        (aa / "config").write_text("harness: claude-code\n")

        warnings_list: list[str] = []

        mock_store = MagicMock()
        mock_store.delete_repo_rows.side_effect = RuntimeError("db locked")

        with patch(
            "agentalloy.storage.state_store.process_store",
            return_value=mock_store,
        ):
            _unwire_repo_local(repo, set(), warnings_list, remove_lifecycle=True)

        assert any("store rows" in w for w in warnings_list)
