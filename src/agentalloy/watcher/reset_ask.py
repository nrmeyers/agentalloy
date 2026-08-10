"""Post-merge reset prompt for the ship watcher.

After merge is detected, asks the user if they want to reset to intake.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ResetPrompter:
    """Handles the post-merge reset-to-intake prompt."""

    def __init__(
        self,
        store: Any,
        lifecycle: Any,
    ) -> None:
        self._store = store
        self._lifecycle = lifecycle

    def ask_and_reset(self, task_slug: str) -> None:
        """Ask the user if they want to reset to intake after merge.

        In the watcher context, we emit a marker file that the proxy
        picks up as a confirm directive. The LLM then presents this
        to the user.
        """
        # Mark as merged
        self._lifecycle.set_merged()

        # Emit a reset marker that the proxy's confirm directive system
        # will pick up on the next signal
        reset_marker_dir = Path(".agentalloy")
        reset_marker_dir.mkdir(exist_ok=True)
        marker_file = reset_marker_dir / f"reset-to-intake-{task_slug}.pending"
        try:
            marker_file.write_text(
                f"PR for task {task_slug} has merged.\n"
                f"Ask the user whether they are ready to reset to intake for the next work item.\n"
                f"Use `agentalloy phase set intake` after they confirm."
            )
            logger.info("Reset marker written for task %r", task_slug)
        except OSError as e:
            logger.warning("Failed to write reset marker: %s", e)

        # Also update the lifecycle to note it's paused awaiting user
        self._lifecycle._store.upsert_pr_lifecycle(
            task_slug,
            paused=1,
            paused_reason="awaiting user reset confirmation",
        )
