"""Unit tests for ``configure_logging`` (#8 — LOG_LEVEL was silently ignored by
every entrypoint because uvicorn's ``--log-level`` only touches ``uvicorn.*``).

The Stage B verdict INFO line added alongside this fix is exercised by the
HIT-path / survivors-routing cases in ``test_retrieval_domain.py``; here we pin
the helper that actually applies ``LOG_LEVEL`` to the ``agentalloy`` namespace.
"""

from __future__ import annotations

import logging

import pytest

from agentalloy.config import configure_logging


@pytest.fixture(autouse=True)
def _restore_agentalloy_level():
    """Save/restore the ``agentalloy`` logger level so these tests don't bleed."""
    logger = logging.getLogger("agentalloy")
    saved = logger.level
    yield
    logger.setLevel(saved)


def test_configure_logging_sets_agentalloy_level() -> None:
    configure_logging("DEBUG")
    assert logging.getLogger("agentalloy").level == logging.DEBUG


def test_configure_logging_reapplies_on_changed_level() -> None:
    # Idempotent: a later call with a changed level must win even though
    # basicConfig already installed a handler on the first call.
    configure_logging("WARNING")
    configure_logging("DEBUG")
    assert logging.getLogger("agentalloy").level == logging.DEBUG


def test_configure_logging_invalid_level_falls_back_to_info() -> None:
    configure_logging("NOPE")
    assert logging.getLogger("agentalloy").level == logging.INFO


def test_configure_logging_reads_settings_when_level_is_none(monkeypatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "ERROR")
    configure_logging(None)
    assert logging.getLogger("agentalloy").level == logging.ERROR
