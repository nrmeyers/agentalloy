"""Telemetry store for CI check results.

Wraps the state store's ci_telemetry table operations.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from agentalloy.storage.state_store import process_store

logger = logging.getLogger(__name__)


class TelemetryStore:
    """Wraps CI telemetry operations on the ci_telemetry table."""

    def __init__(self, project_root: Path | None = None) -> None:
        self._project_root = project_root or Path.cwd()

    def record_ci_check(
        self,
        pr_url: str,
        check_name: str,
        check_status: str,
        check_conclusion: str | None = None,
        started_at: str | None = None,
        completed_at: str | None = None,
        url: str | None = None,
    ) -> None:
        """Record a CI check result to the state store."""
        state = self._get_state_store()
        if state is None:
            logger.warning("No state store available for CI telemetry")
            return
        try:
            state.record_ci_check(
                pr_url=pr_url,
                check_name=check_name,
                check_status=check_status,
                check_conclusion=check_conclusion,
                started_at=started_at,
                completed_at=completed_at,
                url=url,
            )
        except Exception:
            logger.exception("Failed to record CI telemetry")

    def list_ci_telemetry(self, pr_url: str) -> list[dict[str, Any]]:
        """List all CI telemetry for a PR URL."""
        state = self._get_state_store()
        if state is None:
            return []
        return state.list_ci_telemetry(pr_url)

    def _get_state_store(self) -> Any:
        """Get the state store handle."""
        return process_store()
