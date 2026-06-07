"""Tests for reembed CLI and vector_store FTS rebuild retry."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agentalloy.reembed.cli import (
    EXIT_OK,
)
from agentalloy.reembed.cli import (
    main as reembed_main,
)

# ---------------------------------------------------------------------------
# --rebuild-fts flag
# ---------------------------------------------------------------------------


def test_rebuild_fts_flag_accepted() -> None:
    """--rebuild-fts is accepted as valid CLI (dry-run mode)."""
    with (
        patch("agentalloy.reembed.cli.get_settings") as mock_settings,
        patch("agentalloy.reembed.cli.LadybugStore") as mock_store_cls,
        patch("agentalloy.reembed.cli.open_or_create") as mock_vs_cls,
        patch("agentalloy.reembed.cli._is_service_running", return_value=False),
    ):
        mock_settings.return_value.ladybug_db_path = "/tmp/test/ladybug.db"
        mock_settings.return_value.runtime_embedding_model = "test-model"

        mock_store = MagicMock()
        mock_store_cls.return_value.__enter__ = MagicMock(return_value=mock_store)
        mock_store_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_store.execute.return_value = []

        mock_vs = MagicMock()
        mock_vs_cls.return_value.__enter__ = MagicMock(return_value=mock_vs)
        mock_vs_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_vs.count_embeddings.return_value = 0
        mock_vs.fragment_ids_present.return_value = set()

        # dry-run should exit OK with --rebuild-fts
        code = reembed_main(["--rebuild-fts", "--dry-run"])
        assert code == EXIT_OK


def test_rebuild_fts_runs_when_zero_fragments(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """--rebuild-fts triggers rebuild_fts_index when fragments is empty."""
    # Mock the LadybugStore and vector_store so we can control discovery
    with (
        patch("agentalloy.reembed.cli.LadybugStore") as mock_store_cls,
        patch("agentalloy.reembed.cli.open_or_create") as mock_vs_cls,
        patch("agentalloy.reembed.cli.get_settings") as mock_settings,
        patch("agentalloy.reembed.cli._is_service_running", return_value=False),
    ):
        mock_settings.return_value.ladybug_db_path = str(tmp_path / "ladybug.db")
        mock_settings.return_value.runtime_embedding_model = "test-model"

        # Mock LadybugStore context manager
        mock_store = MagicMock()
        mock_store_cls.return_value.__enter__ = MagicMock(return_value=mock_store)
        mock_store_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_store.execute.return_value = []  # no fragments

        # Mock VectorStore context manager
        mock_vs = MagicMock()
        mock_vs_cls.return_value.__enter__ = MagicMock(return_value=mock_vs)
        mock_vs_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_vs.count_embeddings.return_value = 100
        mock_vs.fragment_ids_present.return_value = set()

        with caplog.at_level(logging.INFO):
            code = reembed_main(["--rebuild-fts"])

        assert code == EXIT_OK
        mock_vs.rebuild_fts_index.assert_called_once()
        assert "running --rebuild-fts only" in caplog.text or "rebuild-fts requested" in caplog.text


def test_no_rebuild_without_flag_when_zero_fragments(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Without --rebuild-fts, rebuild_fts_index is NOT called when fragments is empty."""
    with (
        patch("agentalloy.reembed.cli.LadybugStore") as mock_store_cls,
        patch("agentalloy.reembed.cli.open_or_create") as mock_vs_cls,
        patch("agentalloy.reembed.cli.get_settings") as mock_settings,
        patch("agentalloy.reembed.cli._is_service_running", return_value=False),
    ):
        mock_settings.return_value.ladybug_db_path = str(tmp_path / "ladybug.db")
        mock_settings.return_value.runtime_embedding_model = "test-model"

        mock_store = MagicMock()
        mock_store_cls.return_value.__enter__ = MagicMock(return_value=mock_store)
        mock_store_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_store.execute.return_value = []  # no fragments

        mock_vs = MagicMock()
        mock_vs_cls.return_value.__enter__ = MagicMock(return_value=mock_vs)
        mock_vs_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_vs.count_embeddings.return_value = 100
        mock_vs.fragment_ids_present.return_value = set()

        with caplog.at_level(logging.INFO):
            code = reembed_main([])

        assert code == EXIT_OK
        mock_vs.rebuild_fts_index.assert_not_called()
        assert "nothing to do" in caplog.text


def test_rebuild_fts_exit_ok_on_failure(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """When rebuild_fts_index raises, exit code is still EXIT_OK."""
    with (
        patch("agentalloy.reembed.cli.LadybugStore") as mock_store_cls,
        patch("agentalloy.reembed.cli.open_or_create") as mock_vs_cls,
        patch("agentalloy.reembed.cli.get_settings") as mock_settings,
        patch("agentalloy.reembed.cli._is_service_running", return_value=False),
    ):
        mock_settings.return_value.ladybug_db_path = str(tmp_path / "ladybug.db")
        mock_settings.return_value.runtime_embedding_model = "test-model"

        mock_store = MagicMock()
        mock_store_cls.return_value.__enter__ = MagicMock(return_value=mock_store)
        mock_store_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_store.execute.return_value = []

        mock_vs = MagicMock()
        mock_vs_cls.return_value.__enter__ = MagicMock(return_value=mock_vs)
        mock_vs_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_vs.count_embeddings.return_value = 100
        mock_vs.fragment_ids_present.return_value = set()
        mock_vs.rebuild_fts_index.side_effect = Exception("stopwords has been deleted")

        with caplog.at_level(logging.WARNING):
            code = reembed_main(["--rebuild-fts"])

        assert code == EXIT_OK
        assert "BM25 leg degraded" in caplog.text


# ---------------------------------------------------------------------------
# Service management
# ---------------------------------------------------------------------------


def test_detect_service_manager_linux() -> None:
    """_detect_service_manager returns 'systemd' on Linux with systemctl."""
    from agentalloy.reembed.cli import (
        _detect_service_manager,  # pyright: ignore[reportPrivateUsage]
    )

    with (
        patch("platform.system", return_value="Linux"),
        patch("shutil.which", return_value="/usr/bin/systemctl"),
    ):
        assert _detect_service_manager() == "systemd"


def test_detect_service_manager_macos() -> None:
    """_detect_service_manager returns 'launchd' on macOS with launchctl."""
    from agentalloy.reembed.cli import (
        _detect_service_manager,  # pyright: ignore[reportPrivateUsage]
    )

    with (
        patch("platform.system", return_value="Darwin"),
        patch("shutil.which", return_value="/bin/launchctl"),
    ):
        assert _detect_service_manager() == "launchd"


def test_detect_service_manager_none() -> None:
    """_detect_service_manager returns None when no service manager found."""
    from agentalloy.reembed.cli import (
        _detect_service_manager,  # pyright: ignore[reportPrivateUsage]
    )

    with (
        patch("platform.system", return_value="Linux"),
        patch("shutil.which", return_value=None),
    ):
        assert _detect_service_manager() is None


def test_is_service_running_systemd() -> None:
    """_is_service_running returns True when systemd reports active."""
    from agentalloy.reembed.cli import _is_service_running  # pyright: ignore[reportPrivateUsage]

    mock_result = MagicMock()
    mock_result.stdout = "active\n"
    mock_result.returncode = 0

    with (
        patch("platform.system", return_value="Linux"),
        patch("shutil.which", return_value="/usr/bin/systemctl"),
        patch("subprocess.run", return_value=mock_result),
    ):
        assert _is_service_running() is True


def test_is_service_running_systemd_inactive() -> None:
    """_is_service_running returns False when systemd reports inactive."""
    from agentalloy.reembed.cli import _is_service_running  # pyright: ignore[reportPrivateUsage]

    mock_result = MagicMock()
    mock_result.stdout = "inactive\n"
    mock_result.returncode = 3

    with (
        patch("platform.system", return_value="Linux"),
        patch("shutil.which", return_value="/usr/bin/systemctl"),
        patch("subprocess.run", return_value=mock_result),
    ):
        assert _is_service_running() is False


def test_is_service_running_no_service_manager() -> None:
    """_is_service_running returns False when no service manager found."""
    from agentalloy.reembed.cli import _is_service_running  # pyright: ignore[reportPrivateUsage]

    with (
        patch("platform.system", return_value="Linux"),
        patch("shutil.which", return_value=None),
    ):
        assert _is_service_running() is False


def test_stop_service_systemd() -> None:
    """_stop_service stops the systemd service."""
    from agentalloy.reembed.cli import _stop_service  # pyright: ignore[reportPrivateUsage]

    with (
        patch("platform.system", return_value="Linux"),
        patch("shutil.which", return_value="/usr/bin/systemctl"),
        patch("subprocess.run") as mock_run,
    ):
        result = _stop_service()
        assert result is True
        mock_run.assert_called_once()
        assert "systemctl" in mock_run.call_args[0][0]
        assert "--user" in mock_run.call_args[0][0]
        assert "stop" in mock_run.call_args[0][0]


def test_stop_service_systemd_failure() -> None:
    """_stop_service returns False when systemd stop fails."""
    from agentalloy.reembed.cli import _stop_service  # pyright: ignore[reportPrivateUsage]

    with (
        patch("platform.system", return_value="Linux"),
        patch("shutil.which", return_value="/usr/bin/systemctl"),
        patch("subprocess.run", side_effect=OSError("no DBUS")),
    ):
        result = _stop_service()
        assert result is False


def test_restart_service_systemd() -> None:
    """_restart_service starts the systemd service."""
    from agentalloy.reembed.cli import _restart_service  # pyright: ignore[reportPrivateUsage]

    with (
        patch("platform.system", return_value="Linux"),
        patch("shutil.which", return_value="/usr/bin/systemctl"),
        patch("subprocess.run") as mock_run,
    ):
        _restart_service()
        mock_run.assert_called_once()
        assert "systemctl" in mock_run.call_args[0][0]
        assert "--user" in mock_run.call_args[0][0]
        assert "start" in mock_run.call_args[0][0]


# ---------------------------------------------------------------------------
# Pre-flight service stop/restart integration
# ---------------------------------------------------------------------------


def test_reembed_stops_and_restarts_service(tmp_path: Path) -> None:
    """reembed stops service before DB access and restarts after (unless --no-restart)."""
    with (
        patch("agentalloy.reembed.cli.LadybugStore") as mock_store_cls,
        patch("agentalloy.reembed.cli.open_or_create") as mock_vs_cls,
        patch("agentalloy.reembed.cli.get_settings") as mock_settings,
        patch("agentalloy.reembed.cli._is_service_running", return_value=True),
        patch("agentalloy.reembed.cli._stop_service", return_value=True) as mock_stop,
        patch("agentalloy.reembed.cli._restart_service") as mock_restart,
    ):
        mock_settings.return_value.ladybug_db_path = str(tmp_path / "ladybug.db")
        mock_settings.return_value.runtime_embedding_model = "test-model"

        mock_store = MagicMock()
        mock_store_cls.return_value.__enter__ = MagicMock(return_value=mock_store)
        mock_store_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_store.execute.return_value = []

        mock_vs = MagicMock()
        mock_vs_cls.return_value.__enter__ = MagicMock(return_value=mock_vs)
        mock_vs_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_vs.count_embeddings.return_value = 100
        mock_vs.fragment_ids_present.return_value = set()

        code = reembed_main(["--rebuild-fts"])

        assert code == EXIT_OK
        mock_stop.assert_called_once()
        mock_restart.assert_called_once()


def test_reembed_no_restart_flag(tmp_path: Path) -> None:
    """--no-restart prevents automatic service restart."""
    with (
        patch("agentalloy.reembed.cli.LadybugStore") as mock_store_cls,
        patch("agentalloy.reembed.cli.open_or_create") as mock_vs_cls,
        patch("agentalloy.reembed.cli.get_settings") as mock_settings,
        patch("agentalloy.reembed.cli._is_service_running", return_value=True),
        patch("agentalloy.reembed.cli._stop_service", return_value=True),
        patch("agentalloy.reembed.cli._restart_service") as mock_restart,
    ):
        mock_settings.return_value.ladybug_db_path = str(tmp_path / "ladybug.db")
        mock_settings.return_value.runtime_embedding_model = "test-model"

        mock_store = MagicMock()
        mock_store_cls.return_value.__enter__ = MagicMock(return_value=mock_store)
        mock_store_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_store.execute.return_value = []

        mock_vs = MagicMock()
        mock_vs_cls.return_value.__enter__ = MagicMock(return_value=mock_vs)
        mock_vs_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_vs.count_embeddings.return_value = 100
        mock_vs.fragment_ids_present.return_value = set()

        code = reembed_main(["--rebuild-fts", "--no-restart"])

        assert code == EXIT_OK
        mock_restart.assert_not_called()


def test_reembed_no_service_skip_stop(tmp_path: Path) -> None:
    """When service is not running, skip stop and restart steps."""
    with (
        patch("agentalloy.reembed.cli.LadybugStore") as mock_store_cls,
        patch("agentalloy.reembed.cli.open_or_create") as mock_vs_cls,
        patch("agentalloy.reembed.cli.get_settings") as mock_settings,
        patch("agentalloy.reembed.cli._is_service_running", return_value=False),
        patch("agentalloy.reembed.cli._stop_service") as mock_stop,
        patch("agentalloy.reembed.cli._restart_service") as mock_restart,
    ):
        mock_settings.return_value.ladybug_db_path = str(tmp_path / "ladybug.db")
        mock_settings.return_value.runtime_embedding_model = "test-model"

        mock_store = MagicMock()
        mock_store_cls.return_value.__enter__ = MagicMock(return_value=mock_store)
        mock_store_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_store.execute.return_value = []

        mock_vs = MagicMock()
        mock_vs_cls.return_value.__enter__ = MagicMock(return_value=mock_vs)
        mock_vs_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_vs.count_embeddings.return_value = 100
        mock_vs.fragment_ids_present.return_value = set()

        code = reembed_main(["--rebuild-fts"])

        assert code == EXIT_OK
        mock_stop.assert_not_called()
        mock_restart.assert_not_called()


def test_reembed_restart_on_error(tmp_path: Path) -> None:
    """Service is restarted even when reembed fails (DB error)."""
    with (
        patch("agentalloy.reembed.cli.LadybugStore") as mock_store_cls,
        patch("agentalloy.reembed.cli.open_or_create") as mock_vs_cls,
        patch("agentalloy.reembed.cli.get_settings") as mock_settings,
        patch("agentalloy.reembed.cli._is_service_running", return_value=True),
        patch("agentalloy.reembed.cli._stop_service", return_value=True),
        patch("agentalloy.reembed.cli._restart_service") as mock_restart,
    ):
        mock_settings.return_value.ladybug_db_path = str(tmp_path / "ladybug.db")
        mock_settings.return_value.runtime_embedding_model = "test-model"

        mock_store = MagicMock()
        mock_store_cls.return_value.__enter__ = MagicMock(return_value=mock_store)
        mock_store_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_store.execute.return_value = []

        mock_vs = MagicMock()
        mock_vs_cls.return_value.__enter__ = MagicMock(return_value=mock_vs)
        mock_vs_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_vs.count_embeddings.return_value = 100
        mock_vs.fragment_ids_present.return_value = set()
        # Simulate FTS rebuild failure
        mock_vs.rebuild_fts_index.side_effect = Exception("DB error")

        code = reembed_main(["--rebuild-fts"])

        # Still exits OK (FTS failure is non-fatal)
        assert code == EXIT_OK
        # But service is still restarted
        mock_restart.assert_called_once()


# ---------------------------------------------------------------------------
# macOS launchctl PID parsing
# ---------------------------------------------------------------------------


def test_is_service_running_macos_with_pid() -> None:
    """_is_service_running returns True when launchctl list shows a real PID."""
    from agentalloy.reembed.cli import _is_service_running  # pyright: ignore[reportPrivateUsage]

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "12345\t0\tai.agentalloy\n"

    with (
        patch("platform.system", return_value="Darwin"),
        patch("shutil.which", return_value="/bin/launchctl"),
        patch("pathlib.Path.exists", return_value=True),
        patch("subprocess.run", return_value=mock_result),
    ):
        assert _is_service_running() is True


def test_is_service_running_macos_not_running() -> None:
    """_is_service_running returns False when launchctl list shows PID='-'."""
    from agentalloy.reembed.cli import _is_service_running  # pyright: ignore[reportPrivateUsage]

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "-\t0\tai.agentalloy\n"

    with (
        patch("platform.system", return_value="Darwin"),
        patch("shutil.which", return_value="/bin/launchctl"),
        patch("pathlib.Path.exists", return_value=True),
        patch("subprocess.run", return_value=mock_result),
    ):
        assert _is_service_running() is False


# ---------------------------------------------------------------------------
# Dry-run with service running
# ---------------------------------------------------------------------------


def test_reembed_dry_run_stops_service(tmp_path: Path) -> None:
    """--dry-run still stops the service to avoid DB lock conflicts."""
    with (
        patch("agentalloy.reembed.cli.LadybugStore") as mock_store_cls,
        patch("agentalloy.reembed.cli.open_or_create") as mock_vs_cls,
        patch("agentalloy.reembed.cli.get_settings") as mock_settings,
        patch("agentalloy.reembed.cli._is_service_running", return_value=True),
        patch("agentalloy.reembed.cli._stop_service", return_value=True) as mock_stop,
        patch("agentalloy.reembed.cli._restart_service") as mock_restart,
    ):
        mock_settings.return_value.ladybug_db_path = str(tmp_path / "ladybug.db")
        mock_settings.return_value.runtime_embedding_model = "test-model"

        mock_store = MagicMock()
        mock_store_cls.return_value.__enter__ = MagicMock(return_value=mock_store)
        mock_store_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_store.execute.return_value = []

        mock_vs = MagicMock()
        mock_vs_cls.return_value.__enter__ = MagicMock(return_value=mock_vs)
        mock_vs_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_vs.count_embeddings.return_value = 100
        mock_vs.fragment_ids_present.return_value = set()

        code = reembed_main(["--dry-run"])

        assert code == EXIT_OK
        mock_stop.assert_called_once()
        mock_restart.assert_called_once()


# ---------------------------------------------------------------------------
# FTS rebuild warning includes remediation hint
# ---------------------------------------------------------------------------


def test_fts_rebuild_warning_includes_hint(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """FTS rebuild failure warning includes actionable remediation hint."""
    with (
        patch("agentalloy.reembed.cli.LadybugStore") as mock_store_cls,
        patch("agentalloy.reembed.cli.open_or_create") as mock_vs_cls,
        patch("agentalloy.reembed.cli.get_settings") as mock_settings,
        patch("agentalloy.reembed.cli._is_service_running", return_value=False),
    ):
        mock_settings.return_value.ladybug_db_path = str(tmp_path / "ladybug.db")
        mock_settings.return_value.runtime_embedding_model = "test-model"

        mock_store = MagicMock()
        mock_store_cls.return_value.__enter__ = MagicMock(return_value=mock_store)
        mock_store_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_store.execute.return_value = []

        mock_vs = MagicMock()
        mock_vs_cls.return_value.__enter__ = MagicMock(return_value=mock_vs)
        mock_vs_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_vs.count_embeddings.return_value = 100
        mock_vs.fragment_ids_present.return_value = set()
        mock_vs.rebuild_fts_index.side_effect = Exception("stopwords has been deleted")

        with caplog.at_level(logging.WARNING):
            code = reembed_main(["--rebuild-fts"])

        assert code == EXIT_OK
        assert "BM25 leg degraded" in caplog.text
        assert "re-run" in caplog.text or "rebuild-fts" in caplog.text


# ---------------------------------------------------------------------------
# Container-aware service management
# ---------------------------------------------------------------------------


def test_container_stop_restart_success(tmp_path: Path, capsys) -> None:
    """Container stop/restart is called when inside a container."""
    with (
        patch("agentalloy.reembed.cli.is_in_container", return_value=True),
        patch("agentalloy.reembed.cli.stop_service_in_container", return_value=True),
        patch("agentalloy.reembed.cli.restart_service_in_container", return_value=True),
        patch("agentalloy.reembed.cli.LadybugStore") as mock_store_cls,
        patch("agentalloy.reembed.cli.open_or_create") as mock_vs_cls,
        patch("agentalloy.reembed.cli.get_settings") as mock_settings,
        patch("agentalloy.reembed.cli._is_service_running", return_value=False),
    ):
        mock_settings.return_value.ladybug_db_path = str(tmp_path / "ladybug.db")
        mock_settings.return_value.runtime_embedding_model = "test-model"

        mock_store = MagicMock()
        mock_store_cls.return_value.__enter__ = MagicMock(return_value=mock_store)
        mock_store_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_store.execute.return_value = []

        mock_vs = MagicMock()
        mock_vs_cls.return_value.__enter__ = MagicMock(return_value=mock_vs)
        mock_vs_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_vs.count_embeddings.return_value = 100
        mock_vs.fragment_ids_present.return_value = set()

        code = reembed_main(["--rebuild-fts"])

        assert code == EXIT_OK
        captured = capsys.readouterr()
        assert (
            "Stopping agentalloy service (container mode) to release database locks..."
            in captured.err
        )
        assert "Operation complete, restarting agentalloy service..." in captured.err


def test_container_restart_failure_logs_warning(tmp_path: Path) -> None:
    """Container restart failure logs a warning but does not override the operation result."""
    with (
        patch("agentalloy.reembed.cli.is_in_container", return_value=True),
        patch("agentalloy.reembed.cli.stop_service_in_container", return_value=True),
        patch("agentalloy.reembed.cli.restart_service_in_container", return_value=False),
        patch("agentalloy.reembed.cli.LadybugStore") as mock_store_cls,
        patch("agentalloy.reembed.cli.open_or_create") as mock_vs_cls,
        patch("agentalloy.reembed.cli.get_settings") as mock_settings,
        patch("agentalloy.reembed.cli._is_service_running", return_value=False),
    ):
        mock_settings.return_value.ladybug_db_path = str(tmp_path / "ladybug.db")
        mock_settings.return_value.runtime_embedding_model = "test-model"

        mock_store = MagicMock()
        mock_store_cls.return_value.__enter__ = MagicMock(return_value=mock_store)
        mock_store_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_store.execute.return_value = []

        mock_vs = MagicMock()
        mock_vs_cls.return_value.__enter__ = MagicMock(return_value=mock_vs)
        mock_vs_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_vs.count_embeddings.return_value = 100
        mock_vs.fragment_ids_present.return_value = set()

        code = reembed_main(["--rebuild-fts"])

        # Operation should succeed even if restart fails
        assert code == EXIT_OK


# ---------------------------------------------------------------------------
# --no-restart flag with container mode
# ---------------------------------------------------------------------------


def test_container_no_restart_skips_stop_and_restart(tmp_path: Path, capsys) -> None:
    """--no-restart skips both container stop and restart."""
    with (
        patch("agentalloy.reembed.cli.is_in_container", return_value=True),
        patch("agentalloy.reembed.cli.stop_service_in_container") as mock_stop,
        patch("agentalloy.reembed.cli.restart_service_in_container") as mock_restart,
        patch("agentalloy.reembed.cli.LadybugStore") as mock_store_cls,
        patch("agentalloy.reembed.cli.open_or_create") as mock_vs_cls,
        patch("agentalloy.reembed.cli.get_settings") as mock_settings,
        patch("agentalloy.reembed.cli._is_service_running", return_value=False),
    ):
        mock_settings.return_value.ladybug_db_path = str(tmp_path / "ladybug.db")
        mock_settings.return_value.runtime_embedding_model = "test-model"

        mock_store = MagicMock()
        mock_store_cls.return_value.__enter__ = MagicMock(return_value=mock_store)
        mock_store_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_store.execute.return_value = []

        mock_vs = MagicMock()
        mock_vs_cls.return_value.__enter__ = MagicMock(return_value=mock_vs)
        mock_vs_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_vs.count_embeddings.return_value = 100
        mock_vs.fragment_ids_present.return_value = set()

        code = reembed_main(["--rebuild-fts", "--no-restart"])

        assert code == EXIT_OK
        captured = capsys.readouterr()
        # Container stop should NOT appear in output
        assert "Stopping agentalloy service (container mode)" not in captured.err
        # Container restart should NOT appear in output
        assert "Operation complete, restarting agentalloy service" not in captured.err
        # Container service functions should not be called
        mock_stop.assert_not_called()
        mock_restart.assert_not_called()


def test_container_no_restart_skips_service_manager_restart(tmp_path: Path) -> None:
    """--no-restart skips both container and service-manager restart."""
    with (
        patch("agentalloy.reembed.cli.is_in_container", return_value=True),
        patch("agentalloy.reembed.cli.stop_service_in_container", return_value=False),
        patch("agentalloy.reembed.cli.restart_service_in_container", return_value=True),
        patch("agentalloy.reembed.cli.LadybugStore") as mock_store_cls,
        patch("agentalloy.reembed.cli.open_or_create") as mock_vs_cls,
        patch("agentalloy.reembed.cli.get_settings") as mock_settings,
        patch("agentalloy.reembed.cli._is_service_running", return_value=True),
        patch("agentalloy.reembed.cli._stop_service", return_value=True),
        patch("agentalloy.reembed.cli._restart_service") as mock_restart,
    ):
        mock_settings.return_value.ladybug_db_path = str(tmp_path / "ladybug.db")
        mock_settings.return_value.runtime_embedding_model = "test-model"

        mock_store = MagicMock()
        mock_store_cls.return_value.__enter__ = MagicMock(return_value=mock_store)
        mock_store_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_store.execute.return_value = []

        mock_vs = MagicMock()
        mock_vs_cls.return_value.__enter__ = MagicMock(return_value=mock_vs)
        mock_vs_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_vs.count_embeddings.return_value = 100
        mock_vs.fragment_ids_present.return_value = set()

        code = reembed_main(["--rebuild-fts", "--no-restart"])

        assert code == EXIT_OK
        # Service manager restart should also be skipped
        mock_restart.assert_not_called()


def test_container_without_no_restart_calls_stop_and_restart(tmp_path: Path, capsys) -> None:
    """Without --no-restart, container stop and restart are called."""
    with (
        patch("agentalloy.reembed.cli.is_in_container", return_value=True),
        patch("agentalloy.reembed.cli.stop_service_in_container", return_value=True),
        patch("agentalloy.reembed.cli.restart_service_in_container", return_value=True),
        patch("agentalloy.reembed.cli.LadybugStore") as mock_store_cls,
        patch("agentalloy.reembed.cli.open_or_create") as mock_vs_cls,
        patch("agentalloy.reembed.cli.get_settings") as mock_settings,
        patch("agentalloy.reembed.cli._is_service_running", return_value=False),
    ):
        mock_settings.return_value.ladybug_db_path = str(tmp_path / "ladybug.db")
        mock_settings.return_value.runtime_embedding_model = "test-model"

        mock_store = MagicMock()
        mock_store_cls.return_value.__enter__ = MagicMock(return_value=mock_store)
        mock_store_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_store.execute.return_value = []

        mock_vs = MagicMock()
        mock_vs_cls.return_value.__enter__ = MagicMock(return_value=mock_vs)
        mock_vs_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_vs.count_embeddings.return_value = 100
        mock_vs.fragment_ids_present.return_value = set()

        code = reembed_main(["--rebuild-fts"])

        assert code == EXIT_OK
        captured = capsys.readouterr()
        assert "Stopping agentalloy service (container mode)" in captured.err
        assert "Operation complete, restarting agentalloy service" in captured.err
