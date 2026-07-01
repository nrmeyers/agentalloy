"""Tests for the reembed CLI and the Lance FTS rebuild path.

v5 note: reembed no longer stops/restarts the agentalloy service. Lance is MVCC
(atomic versioned writes) and telemetry lives in a separate ``telemetry.duck``,
so a reembed is a live, zero-downtime operation (decisions D3/D4). The old
service-manager detection (``_detect_service_manager``), running-state probe
(``_is_service_running``), and stop/restart helpers (``_stop_service`` /
``_restart_service``), plus the container stop/restart integration in this CLI,
were DELETED — so all tests that asserted those behaviours are gone. ``--no-restart``
is now accepted-but-ignored. The CLI opens its stores via
``open_skills`` / ``open_fragments`` (not ``LadybugStore`` / ``open_or_create``).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
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
# Shared store mocking
# ---------------------------------------------------------------------------


@contextmanager
def _patched_stores(*, count_embeddings: int = 100) -> Iterator[tuple[MagicMock, MagicMock]]:
    """Patch the CLI's store openers + settings. Yields (skill_store, fragment_store).

    The skill store's ``execute`` returns no rows (no fragments discovered), and
    the fragment store reports an empty ``fragment_ids_present`` so discovery is
    a clean no-op — every test here exercises the rebuild-fts / metadata path,
    not real embedding.
    """
    with (
        patch("agentalloy.reembed.cli.open_skills") as mock_open_skills,
        patch("agentalloy.reembed.cli.open_fragments") as mock_open_fragments,
        patch("agentalloy.reembed.cli.get_settings") as mock_settings,
    ):
        mock_settings.return_value.runtime_embedding_model = "test-model"
        mock_store = MagicMock()
        mock_store.execute.return_value = []  # no active fragments
        mock_open_skills.return_value = mock_store

        mock_vs = MagicMock()
        mock_vs.count_embeddings.return_value = count_embeddings
        mock_vs.fragment_ids_present.return_value = set()
        mock_open_fragments.return_value = mock_vs

        yield mock_store, mock_vs


# ---------------------------------------------------------------------------
# --rebuild-fts flag
# ---------------------------------------------------------------------------


def test_rebuild_fts_flag_accepted() -> None:
    """--rebuild-fts is accepted as valid CLI (dry-run mode)."""
    with _patched_stores(count_embeddings=0):
        # dry-run short-circuits before any FTS rebuild; just exits OK.
        code = reembed_main(["--rebuild-fts", "--dry-run"])
        assert code == EXIT_OK


def test_rebuild_fts_runs_when_zero_fragments(caplog: pytest.LogCaptureFixture) -> None:
    """--rebuild-fts triggers rebuild_fts_index when there's nothing to embed."""
    with _patched_stores() as (mock_store, mock_vs):
        with caplog.at_level(logging.INFO):
            code = reembed_main(["--rebuild-fts"])

        assert code == EXIT_OK
        mock_vs.rebuild_fts_index.assert_called_once()
        assert "running --rebuild-fts only" in caplog.text or "rebuild-fts requested" in caplog.text
        # Every pass stamps the corpus schema version into corpus_meta (on the
        # SkillStore now) — even the zero-fragment / idempotent path — so existing
        # corpora pick up the explicit marker without a full re-embed.
        from agentalloy.storage.card_index import (
            CORPUS_SCHEMA_VERSION,
            META_KEY_SCHEMA_VERSION,
        )

        mock_store.set_meta.assert_any_call(META_KEY_SCHEMA_VERSION, str(CORPUS_SCHEMA_VERSION))


def test_no_rebuild_without_flag_when_zero_fragments(caplog: pytest.LogCaptureFixture) -> None:
    """Without --rebuild-fts, rebuild_fts_index is NOT called when nothing to embed."""
    with _patched_stores() as (_mock_store, mock_vs):
        with caplog.at_level(logging.INFO):
            code = reembed_main([])

        assert code == EXIT_OK
        mock_vs.rebuild_fts_index.assert_not_called()
        assert "nothing to do" in caplog.text


def test_rebuild_fts_exit_ok_on_failure(caplog: pytest.LogCaptureFixture) -> None:
    """When rebuild_fts_index raises, exit code is still EXIT_OK (BM25 leg degrades)."""
    with _patched_stores() as (_mock_store, mock_vs):
        mock_vs.rebuild_fts_index.side_effect = Exception("stopwords has been deleted")

        with caplog.at_level(logging.WARNING):
            code = reembed_main(["--rebuild-fts"])

        assert code == EXIT_OK
        assert "BM25 leg degraded" in caplog.text


def test_no_restart_flag_accepted_but_ignored(caplog: pytest.LogCaptureFixture) -> None:
    """--no-restart is accepted for backward compatibility and changes nothing.

    v5 reembed never stops/restarts the service, so the flag is a documented
    no-op; the pass still runs and stamps metadata normally.
    """
    with _patched_stores() as (mock_store, mock_vs):
        with caplog.at_level(logging.INFO):
            code = reembed_main(["--rebuild-fts", "--no-restart"])

        assert code == EXIT_OK
        mock_vs.rebuild_fts_index.assert_called_once()


# ---------------------------------------------------------------------------
# FTS rebuild warning (v5: no remediation hint — the BM25 leg simply degrades)
# ---------------------------------------------------------------------------


def test_fts_rebuild_warning_emitted_on_failure(caplog: pytest.LogCaptureFixture) -> None:
    """FTS rebuild failure logs a 'BM25 leg degraded' warning (non-fatal)."""
    with _patched_stores() as (_mock_store, mock_vs):
        mock_vs.rebuild_fts_index.side_effect = Exception("stopwords has been deleted")

        with caplog.at_level(logging.WARNING):
            code = reembed_main(["--rebuild-fts"])

        assert code == EXIT_OK
        # v5 dropped the old "agentalloy server-stop" / re-run remediation hint
        # from this warning — the failure is benign (Lance manages FTS) and the
        # message is just the degraded-leg notice.
        assert "BM25 leg degraded" in caplog.text


# ---------------------------------------------------------------------------
# Lock-held error recognition (issue #84 — remediation reworded for v5)
# ---------------------------------------------------------------------------


def test_lock_held_error_returns_exit_db_with_remediation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A DuckDB single-writer lock failure (a concurrent ingest/reembed holding
    agentalloy.duck) must exit EXIT_DB with a targeted remediation instead of an
    unhandled traceback. In v5 the remediation is 'wait and re-run' (the writer
    lock is benign + transient), not 'stop the service'."""
    from agentalloy.reembed.cli import EXIT_DB

    lock_err = RuntimeError(
        "IO Error: Could not set lock on file 'agentalloy.duck.lock': "
        "Conflicting lock is held by PID 12345"
    )
    with (
        patch("agentalloy.reembed.cli.open_skills", side_effect=lock_err),
        patch("agentalloy.reembed.cli.get_settings") as mock_settings,
    ):
        mock_settings.return_value.runtime_embedding_model = "test-model"
        code = reembed_main([])

    assert code == EXIT_DB
    err = capsys.readouterr().err
    assert "Another process holds the corpus DB lock" in err
    assert "re-run the command" in err


def test_non_lock_db_error_still_raises() -> None:
    """Only lock-held errors are translated; other DB failures propagate."""
    with (
        patch("agentalloy.reembed.cli.open_skills", side_effect=RuntimeError("corrupt")),
        patch("agentalloy.reembed.cli.get_settings") as mock_settings,
    ):
        mock_settings.return_value.runtime_embedding_model = "test-model"
        with pytest.raises(RuntimeError, match="corrupt"):
            reembed_main([])
