"""Automated CI watcher for the ship phase.

Opens a PR, monitors CI checks every 60 seconds, captures telemetry,
detects merge completion, and triggers a reset-to-intake prompt after merge.

Public API
----------
``start_watcher(task_slug, branch, base_branch, title, body, **opts)``
    Start the watcher. Returns a ``WatcherHandle`` you can call ``.stop()`` on.

``stop_watcher(task_slug)``
    Stop a running watcher by task slug (useful from tests).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from agentalloy.watcher.ci_monitor import CICheckMonitor
from agentalloy.watcher.lifecycle import LifecycleManager
from agentalloy.watcher.pr_opener import PRManager
from agentalloy.watcher.reset_ask import ResetPrompter
from agentalloy.watcher.telemetry import TelemetryStore

logger = logging.getLogger(__name__)


@dataclass
class WatcherHandle:
    """Handle to a running watcher thread."""

    task_slug: str
    _stop_event: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None
    _running: bool = False

    @property
    def is_running(self) -> bool:
        return self._running

    def stop(self) -> None:
        """Signal the watcher to stop."""
        logger.info("Stopping watcher for task %r", self.task_slug)
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=30)
            self._running = False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Registry of running watchers keyed by task_slug.
# In production this lives in process memory; tests can call stop_watcher().
_running_watchers: dict[str, WatcherHandle] = {}


def start_watcher(
    task_slug: str,
    branch: str,
    base_branch: str,
    title: str,
    body: str,
    *,
    project_root: Path | None = None,
    ci_interval: int = 60,
    max_failures: int = 5,
    auto_merge: bool = False,
) -> WatcherHandle:
    """Start the automated CI watcher for a ship task.

    Parameters
    ----------
    task_slug:
        The SDD task slug (e.g. ``"add-telemetry"``).
    branch:
        The feature branch to open the PR against.
    base_branch:
        The target branch (usually ``"main"``).
    title:
        PR title.
    body:
        PR body / narrative.
    project_root:
        Project root path (for CLI detection). Defaults to ``cwd``.
    ci_interval:
        Seconds between CI polls (default 60).
    max_failures:
        Pause after this many CI check failures (default 5).
    auto_merge:
        Whether to enable auto-merge on the PR.
    """
    handle = WatcherHandle(task_slug=task_slug)
    _running_watchers[task_slug] = handle

    # Shared components
    store = TelemetryStore(project_root=project_root)
    lifecycle = LifecycleManager(store=store, task_slug=task_slug)
    pr_manager = PRManager(
        store=store,
        lifecycle=lifecycle,
        project_root=project_root,
    )
    monitor = CICheckMonitor(
        store=store,
        lifecycle=lifecycle,
        max_failures=max_failures,
    )
    reset_prompter = ResetPrompter(
        store=store,
        lifecycle=lifecycle,
    )

    def _run():
        """Watcher loop."""
        try:
            # Phase 1: Open PR
            logger.info("Watcher starting for task %r", task_slug)
            lifecycle.start_watching()

            pr_url = pr_manager.open_pr(
                task_slug=task_slug,
                branch=branch,
                base_branch=base_branch,
                title=title,
                body=body,
                auto_merge=auto_merge,
            )

            if pr_url:
                lifecycle.update_pr_url(pr_url)

                # Phase 2: Poll CI
                while not handle._stop_event.is_set():
                    all_green, check_count = monitor.poll_once(pr_url)

                    if all_green and check_count > 0:
                        # All checks passed — check if merged
                        if lifecycle.is_merged():
                            logger.info("PR %s merged, triggering reset prompt", pr_url)
                            lifecycle.stop_watching()
                            reset_prompter.ask_and_reset(task_slug)
                            break
                        else:
                            # Green but not yet merged — keep polling
                            logger.debug("CI checks green, waiting for merge")
                    else:
                        logger.debug(
                            "CI checks not yet green (green=%s, checks=%d)", all_green, check_count
                        )

                    # Sleep in small increments so we can interrupt
                    for _ in range(ci_interval * 10):
                        if handle._stop_event.is_set():
                            break
                        time.sleep(0.1)
            else:
                logger.error("Failed to open PR for task %r", task_slug)
                lifecycle.update_pr_url("error")
        except Exception:
            logger.exception("Watcher crashed for task %r", task_slug)
        finally:
            handle._running = False
            _running_watchers.pop(task_slug, None)

    handle._thread = threading.Thread(target=_run, name=f"watcher-{task_slug}", daemon=True)
    handle._running = True
    handle._thread.start()
    return handle


def stop_watcher(task_slug: str) -> bool:
    """Stop a running watcher by task slug. Returns True if found and stopped."""
    handle = _running_watchers.pop(task_slug, None)
    if handle and handle.is_running:
        handle.stop()
        return True
    return False
