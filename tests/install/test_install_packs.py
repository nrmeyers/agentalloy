# pyright: reportPrivateUsage=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false
"""Unit tests for the ``install-packs`` subcommand.

Focus: the state-file handoff that prevents the setup wizard and
install-packs from prompting the user twice for the same pack selection.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from agentalloy.install import state as install_state
from agentalloy.install.subcommands.install_packs import (
    _bulk_reembed,
    _clear_pending_pack_selection,
    _ensure_skill_schema,
    _installed_pack_names,
    _load_pending_pack_selection,
    _reclaim_native_corpus_lock,
    _render_install_summary,
    _restart_native_service,
    _select_packs,
    _summarize_install_result,
)

_LOCK_ERR = (
    "RuntimeError: IO exception: Could not set lock on file agentalloy.duck: "
    "Lock is held by PID 12345"
)


@pytest.fixture()
def repo_root(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text("")
    return tmp_path


@pytest.fixture()
def available() -> dict[str, dict[str, object]]:
    """A small pack catalog: one always-on, two tier'd."""
    return {
        "core/foundation": {
            "name": "core/foundation",
            "tier": "foundation",
            "always_install": True,
            "skills": [{"skill_id": "s1"}],
        },
        "lang/python": {
            "name": "lang/python",
            "tier": "language",
            "skills": [{"skill_id": "s2"}, {"skill_id": "s3"}],
        },
        "tool/git": {
            "name": "tool/git",
            "tier": "tooling",
            "skills": [{"skill_id": "s4"}],
        },
    }


class TestPendingSelectionLoader:
    def test_load_returns_none_when_absent(self, repo_root: Path) -> None:
        # Fresh state file has the field defaulted to None.
        st = install_state.load_state(repo_root)
        install_state.save_state(st, repo_root)
        assert _load_pending_pack_selection() is None

    def test_load_returns_persisted_list(self, repo_root: Path) -> None:
        st = install_state.load_state(repo_root)
        install_state.set_pending_pack_selection(st, ["lang/python"])
        install_state.save_state(st, repo_root)
        assert _load_pending_pack_selection() == ["lang/python"]

    def test_load_returns_empty_list_when_explicit_no_extras(self, repo_root: Path) -> None:
        st = install_state.load_state(repo_root)
        install_state.set_pending_pack_selection(st, [])
        install_state.save_state(st, repo_root)
        # Empty list (explicit "no extras") must be distinguishable from None
        # so the priority check in _select_packs honors the user's intent.
        result = _load_pending_pack_selection()
        assert result == []
        assert result is not None


class TestClearPendingSelection:
    def test_clear_after_set(self, repo_root: Path) -> None:
        st = install_state.load_state(repo_root)
        install_state.set_pending_pack_selection(st, ["lang/python"])
        install_state.save_state(st, repo_root)
        _clear_pending_pack_selection()
        st2 = install_state.load_state(repo_root)
        assert install_state.get_pending_pack_selection(st2) is None

    def test_clear_when_nothing_set_is_noop(self, repo_root: Path) -> None:
        # Should not raise even if pending_pack_selection was never set.
        st = install_state.load_state(repo_root)
        install_state.save_state(st, repo_root)
        _clear_pending_pack_selection()  # no exception
        st2 = install_state.load_state(repo_root)
        assert install_state.get_pending_pack_selection(st2) is None


class TestSelectPacksPriority:
    """Priority: --packs flag > pending-state > TTY prompt > defaults."""

    def test_packs_flag_wins_over_pending_state(
        self, repo_root: Path, available: dict[str, dict[str, object]]
    ) -> None:
        # Even though state has a pending selection, the explicit CLI
        # flag must override it (matches the documented contract).
        st = install_state.load_state(repo_root)
        install_state.set_pending_pack_selection(st, ["lang/python"])
        install_state.save_state(st, repo_root)

        selected, _unknown, consumed = _select_packs(
            available, packs_flag="tool/git", interactive=False
        )
        assert "tool/git" in selected
        assert "lang/python" not in selected  # state was ignored
        assert consumed is False  # didn't consume the pending selection

    def test_pending_state_wins_over_interactive(
        self, repo_root: Path, available: dict[str, dict[str, object]]
    ) -> None:
        # When state has a pending selection AND no --packs flag, use the
        # state — do NOT show the interactive prompt.
        st = install_state.load_state(repo_root)
        install_state.set_pending_pack_selection(st, ["lang/python"])
        install_state.save_state(st, repo_root)

        # `interactive=True` would normally trigger the prompt; pending
        # state must short-circuit that path.
        selected, _unknown, consumed = _select_packs(available, packs_flag=None, interactive=True)
        assert "lang/python" in selected
        # Always-on packs are always merged in regardless of source.
        assert "core/foundation" in selected
        assert consumed is True

    def test_pending_empty_list_means_explicit_no_extras(
        self, repo_root: Path, available: dict[str, dict[str, object]]
    ) -> None:
        st = install_state.load_state(repo_root)
        install_state.set_pending_pack_selection(st, [])
        install_state.save_state(st, repo_root)

        selected, _unknown, consumed = _select_packs(available, packs_flag=None, interactive=True)
        # Only the always-on pack — the user said "no extras".
        assert selected == ["core/foundation"]
        assert consumed is True

    def test_no_flag_no_pending_no_tty_returns_always_on_only(
        self, repo_root: Path, available: dict[str, dict[str, object]]
    ) -> None:
        # Non-TTY path with nothing in state: just always-on packs.
        selected, _unknown, consumed = _select_packs(available, packs_flag=None, interactive=False)
        assert selected == ["core/foundation"]
        assert consumed is False

    def test_pending_with_unknown_pack_reports_unknown(
        self, repo_root: Path, available: dict[str, dict[str, object]]
    ) -> None:
        # If the pending list references a pack that no longer exists
        # (e.g., it was removed between setup and ingest), the unknown
        # is reported but the rest still installs.
        st = install_state.load_state(repo_root)
        install_state.set_pending_pack_selection(st, ["lang/python", "gone/missing"])
        install_state.save_state(st, repo_root)
        selected, unknown, consumed = _select_packs(available, packs_flag=None, interactive=False)
        assert "lang/python" in selected
        assert "gone/missing" in unknown
        assert consumed is True


class TestInstalledPackAnnotation:
    """``_installed_pack_names`` powers the [installed] marker in the prompt."""

    def test_returns_empty_when_state_fresh(self, repo_root: Path) -> None:
        assert _installed_pack_names() == set()

    def test_returns_recorded_packs(self, repo_root: Path) -> None:
        st = install_state.load_state(repo_root)
        st["installed_packs"] = ["lang/python", "tool/git"]
        install_state.save_state(st, repo_root)
        assert _installed_pack_names() == {"lang/python", "tool/git"}

    def test_ignores_non_string_entries(self, repo_root: Path) -> None:
        # Defensive against tampered/corrupt state.
        st = install_state.load_state(repo_root)
        st["installed_packs"] = ["lang/python", 42, None, "tool/git"]
        install_state.save_state(st, repo_root)
        assert _installed_pack_names() == {"lang/python", "tool/git"}


class TestEnsureSkillSchema:
    """Schema guard for issue #84: a wiped-then-recreated corpus can have DB
    files on disk without the skill-graph tables; install-packs must migrate
    (idempotently) before ingesting instead of failing every skill with
    "Table skills does not exist"."""

    def test_runs_migrate_on_store(self, tmp_path: Path) -> None:
        with (
            patch("agentalloy.config.get_settings") as mock_settings,
            patch("agentalloy.storage.open.open_skills") as mock_open_skills,
        ):
            mock_settings.return_value.duckdb_path = str(tmp_path / "agentalloy.duck")
            _ensure_skill_schema()
        mock_open_skills.assert_called_once()
        mock_open_skills.return_value.migrate.assert_called_once()

    def test_failure_is_nonfatal_and_warns(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch("agentalloy.config.get_settings") as mock_settings,
            patch(
                "agentalloy.storage.open.open_skills",
                side_effect=RuntimeError("disk on fire"),
            ),
        ):
            mock_settings.return_value.duckdb_path = str(tmp_path / "agentalloy.duck")
            _ensure_skill_schema()  # must not raise
        err = capsys.readouterr().err
        assert "could not verify/create corpus graph schema" in err
        assert "disk on fire" in err

    def test_lock_held_failure_prints_remediation(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch("agentalloy.config.get_settings") as mock_settings,
            patch(
                "agentalloy.storage.open.open_skills",
                side_effect=RuntimeError(_LOCK_ERR),
            ),
        ):
            mock_settings.return_value.duckdb_path = str(tmp_path / "agentalloy.duck")
            _ensure_skill_schema()
        err = capsys.readouterr().err
        assert "Another process holds the corpus DB lock" in err
        assert "writing agentalloy.duck" in err


class TestSummarizeInstallResult:
    """install-packs.json must keep per-skill failure detail (issue #84)."""

    def test_drops_successful_ingest_results(self) -> None:
        result = {
            "action": "ingested",
            "pack": "core",
            "ingest_results": [
                {"yaml": "a.yaml", "outcome": "ingested", "stderr_tail": ""},
                {"yaml": "b.yaml", "outcome": "duplicate", "stderr_tail": ""},
            ],
        }
        out = _summarize_install_result(result)
        assert "ingest_results" not in out
        assert "failed_ingest_results" not in out
        assert out["action"] == "ingested"

    def test_keeps_failed_ingest_detail(self) -> None:
        result = {
            "action": "ingested_with_errors",
            "pack": "core",
            "ingest_results": [
                {"yaml": "ok.yaml", "outcome": "ingested", "exit_code": 0, "stderr_tail": ""},
                {
                    "yaml": "writing-readmes.yaml",
                    "outcome": "failed",
                    "exit_code": 1,
                    "stderr_tail": "RuntimeError: Binder exception: Table Skill does not exist.",
                },
            ],
        }
        out = _summarize_install_result(result)
        assert "ingest_results" not in out
        failed = out["failed_ingest_results"]
        assert failed == [
            {
                "yaml": "writing-readmes.yaml",
                "exit_code": 1,
                "stderr_tail": "RuntimeError: Binder exception: Table Skill does not exist.",
            }
        ]

    def test_caps_failed_detail_at_ten(self) -> None:
        result = {
            "action": "ingested_with_errors",
            "ingest_results": [
                {"yaml": f"s{i}.yaml", "outcome": "failed", "exit_code": 1, "stderr_tail": "boom"}
                for i in range(25)
            ],
        }
        out = _summarize_install_result(result)
        assert len(out["failed_ingest_results"]) == 10


class TestBulkReembedLockHint:
    def test_lock_held_exception_prints_remediation(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch("agentalloy.reembed.cli.main", side_effect=RuntimeError(_LOCK_ERR)):
            rc = _bulk_reembed()
        assert rc == 2
        err = capsys.readouterr().err
        assert "reembed raised" in err
        assert "Another process holds the corpus DB lock" in err

    def test_other_exception_has_no_lock_hint(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch("agentalloy.reembed.cli.main", side_effect=RuntimeError("kaboom")):
            rc = _bulk_reembed()
        assert rc == 2
        err = capsys.readouterr().err
        assert "reembed raised" in err
        assert "corpus DB lock" not in err


class TestIsLockHeldError:
    def test_matches_both_lock_phrases(self) -> None:
        from agentalloy.storage.skill_store import is_lock_held_error

        assert is_lock_held_error("Could not set lock on file /x/agentalloy.duck")
        assert is_lock_held_error("IO Error: Conflicting lock is held by another process")
        assert not is_lock_held_error("Binder exception: Table skills does not exist.")
        assert not is_lock_held_error("")


class TestNativeCorpusLockReclaim:
    """install-packs frees the corpus DB lock from a running native service so a
    direct invocation doesn't spew per-skill lock WARNs and build a partial corpus.
    """

    _SP = "agentalloy.install.server_proc"

    def test_noop_when_port_free(self) -> None:
        """Port free → lock is free → no systemctl, returns False (nothing to restart)."""
        with (
            patch(f"{self._SP}.configured_port", return_value=47950),
            patch(f"{self._SP}.find_listening_pid", return_value=None),
            patch(f"{self._SP}.reclaim_stale_port") as reclaim,
            patch("shutil.which", return_value="/usr/bin/systemctl"),
            patch("subprocess.run") as run,
        ):
            assert _reclaim_native_corpus_lock() is False
            run.assert_not_called()
            reclaim.assert_not_called()

    def test_stops_unit_and_reclaims_when_held(self) -> None:
        """Port held + systemctl present → stop the unit, reclaim the port, return True."""
        from unittest.mock import MagicMock

        with (
            patch(f"{self._SP}.configured_port", return_value=47950),
            patch(f"{self._SP}.find_listening_pid", return_value=648605),
            patch(f"{self._SP}.reclaim_stale_port", return_value=648605) as reclaim,
            patch("shutil.which", return_value="/usr/bin/systemctl"),
            patch("subprocess.run", return_value=MagicMock(returncode=0)) as run,
        ):
            assert _reclaim_native_corpus_lock() is True
            stop_cmd = run.call_args.args[0]
            assert stop_cmd == ["systemctl", "--user", "stop", "agentalloy.service"]
            reclaim.assert_called_once_with(47950, ["uvicorn", "agentalloy.app"])

    def test_manual_launch_not_marked_systemd(self) -> None:
        """Port held but no systemctl (manual launch) → still reclaims, returns False."""
        with (
            patch(f"{self._SP}.configured_port", return_value=47950),
            patch(f"{self._SP}.find_listening_pid", return_value=648605),
            patch(f"{self._SP}.reclaim_stale_port", return_value=648605) as reclaim,
            patch("shutil.which", return_value=None),
            patch("subprocess.run") as run,
        ):
            assert _reclaim_native_corpus_lock() is False
            run.assert_not_called()  # no systemctl to call
            reclaim.assert_called_once()

    def test_restart_invokes_systemctl_start(self) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/systemctl"),
            patch("subprocess.run") as run,
        ):
            _restart_native_service()
            assert run.call_args.args[0] == ["systemctl", "--user", "start", "agentalloy.service"]


class TestInstallSummaryRender:
    """stdout gets a one-line digest by default; the full blob is --json / the file."""

    def _summary(self) -> dict[str, object]:
        return {
            "action": "packs_installed",
            "selected": ["core", "engineering", "sdd"],
            "failed_packs": [],
            "reembed_exit_code": 0,
            "duration_ms": 2363,
            "install_results": [
                {"action": "already_installed", "skills_already_present": 12, "skills_ingested": 0},
                {"action": "ingested", "skills_ingested": 5, "skills_deprecated": 1},
                {"action": "ingested", "skills_ingested": 3, "ingest_failures": 0},
            ],
        }

    def test_one_line_counts(self) -> None:
        line = _render_install_summary(self._summary())
        assert line.startswith("install-packs: 3 packs (2 ingested, 1 already present, 0 failed)")
        assert "+8 ingested" in line  # 5 + 3
        assert "12 present" in line
        assert "1 deprecated" in line
        assert "reembed: ok" in line
        assert "install-packs.json" in line
        assert "\n" not in line  # single line when nothing failed

    def test_failed_packs_listed(self) -> None:
        s = self._summary()
        s["failed_packs"] = ["redis"]
        s["reembed_exit_code"] = 2
        line = _render_install_summary(s)
        assert "1 failed" in line
        assert "reembed: exit 2" in line
        assert "failed packs: redis" in line
