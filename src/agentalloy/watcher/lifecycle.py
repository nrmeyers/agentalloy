"""PR lifecycle management for the ship watcher.

Handles CRUD on the ``pr_lifecycle`` table in the state store.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from agentalloy.storage.state_store import process_store

logger = logging.getLogger(__name__)


class LifecycleManager:
    """Manages PR lifecycle state in the state store."""

    def __init__(self, store: Any, task_slug: str) -> None:
        self._store = store
        self._task_slug = task_slug

    @property
    def task_slug(self) -> str:
        return self._task_slug

    @property
    def _state_store(self) -> Any:
        """Get the state store handle."""
        if self._store is not None:
            return self._store
        return process_store()

    def start_watching(self) -> None:
        """Mark watcher as started."""
        state = self._state_store
        if state is None:
            logger.warning("No state store available for lifecycle start")
            return
        state.upsert_pr_lifecycle(self._task_slug, watcher_started=self._now_iso())

    def stop_watching(self) -> None:
        """Mark watcher as stopped."""
        state = self._state_store
        if state is None:
            return
        state.upsert_pr_lifecycle(self._task_slug, watcher_stopped=self._now_iso())

    def update_pr_url(self, pr_url: str) -> None:
        """Record the PR URL."""
        state = self._state_store
        if state is None:
            return
        state.upsert_pr_lifecycle(self._task_slug, pr_url=pr_url)

    def increment_ci_failures(self) -> int:
        """Increment the CI failure counter. Returns the new count."""
        state = self._state_store
        if state is None:
            return 0
        current = self.get_ci_failures()
        new_count = current + 1
        state.upsert_pr_lifecycle(self._task_slug, ci_failures=new_count)
        return new_count

    def get_ci_failures(self) -> int:
        """Get current CI failure count."""
        state = self._state_store
        if state is None:
            return 0
        info = state.get_pr_lifecycle(self._task_slug)
        if info is None:
            return 0
        return info.get("ci_failures", 0)

    def set_merged(self) -> None:
        """Mark the PR as merged."""
        state = self._state_store
        if state is None:
            return
        state.upsert_pr_lifecycle(self._task_slug, merged_at=self._now_iso())

    def is_merged(self) -> bool:
        """Check if the PR has been merged."""
        state = self._state_store
        if state is None:
            return False
        info = state.get_pr_lifecycle(self._task_slug)
        if info is None:
            return False
        return info.get("merged_at") is not None

    def get_pr_url(self) -> str | None:
        """Get the PR URL."""
        state = self._state_store
        if state is None:
            return None
        info = state.get_pr_lifecycle(self._task_slug)
        if info is None:
            return None
        return info.get("pr_url")

    def get_auto_merge(self) -> bool:
        """Check if auto-merge is enabled."""
        state = self._state_store
        if state is None:
            return False
        info = state.get_pr_lifecycle(self._task_slug)
        if info is None:
            return False
        return bool(info.get("auto_merge", 0))

    def get_status(self) -> dict[str, Any]:
        """Get the full lifecycle status."""
        state = self._state_store
        if state is None:
            return {}
        return state.get_pr_lifecycle(self._task_slug) or {}

    @staticmethod
    def _now_iso() -> str:
        from datetime import datetime
        return datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
