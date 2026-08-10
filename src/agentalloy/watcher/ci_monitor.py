"""CI check polling for the ship watcher.

Polls CI status via GitHub CLI (gh) or GitLab CLI (glab) every N seconds.
Captures results to the ci_telemetry table.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from typing import Any

logger = logging.getLogger(__name__)


class CICheckMonitor:
    """Polls CI check status and captures telemetry."""

    def __init__(
        self,
        store: Any,
        lifecycle: Any,
        max_failures: int = 5,
    ) -> None:
        self._store = store
        self._lifecycle = lifecycle
        self._max_failures = max_failures
        self._vcs_type: str | None = None

    @property
    def vcs_type(self) -> str | None:
        if self._vcs_type is None:
            self._vcs_type = self._detect_vcs()
        return self._vcs_type

    def _detect_vcs(self) -> str | None:
        """Detect which VCS CLI is available."""
        if shutil.which("gh") is not None:
            return "gh"
        if shutil.which("glab") is not None:
            return "glab"
        return None

    def poll_once(self, pr_url: str) -> tuple[bool, int]:
        """Poll CI checks for the given PR URL.

        Returns (all_green, total_check_count).
        """
        vcs = self.vcs_type
        if vcs is None:
            logger.warning("No VCS CLI available for CI polling")
            return False, 0

        if vcs == "gh":
            return self._gh_poll(pr_url)
        else:
            return self._glab_poll(pr_url)

    def _gh_poll(self, pr_url: str) -> tuple[bool, int]:
        """Poll CI checks via GitHub CLI."""
        cmd = ["gh", "pr", "checks", pr_url.split("/")[-1], "--json", "name,state,status,conclusion,startedAt,completedAt,url"]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                logger.warning("gh pr checks failed: %s", result.stderr.strip())
                return False, 0

            checks_data = json.loads(result.stdout)
            checks = checks_data if isinstance(checks_data, list) else []

            total = len(checks)
            all_green = True

            for check in checks:
                status = check.get("state", "")
                conclusion = check.get("conclusion")

                # Record to telemetry
                self._store.record_ci_check(
                    pr_url=pr_url,
                    check_name=check.get("name", ""),
                    check_status=status,
                    check_conclusion=conclusion,
                    started_at=check.get("startedAt"),
                    completed_at=check.get("completedAt"),
                    url=check.get("url"),
                )

                # Determine if this check is green
                if status == "completed" and conclusion in ("success", None):
                    pass  # green
                elif status == "skipped":
                    pass  # skip doesn't count as failure
                else:
                    all_green = False
                    logger.debug("Check %r is %s/%s", check.get("name"), status, conclusion)

            # Track failures
            if not all_green:
                fail_count = self._lifecycle.increment_ci_failures()
                if fail_count >= self._max_failures:
                    logger.warning("CI failure limit reached (%d/%d) for task %r",
                                   fail_count, self._max_failures, self._lifecycle.task_slug)

            return all_green, total

        except json.JSONDecodeError:
            logger.error("Invalid JSON from gh pr checks")
            return False, 0
        except subprocess.TimeoutExpired:
            logger.warning("gh pr checks timed out")
            return False, 0
        except Exception:
            logger.exception("Error polling CI via gh")
            return False, 0

    def _glab_poll(self, pr_url: str) -> tuple[bool, int]:
        """Poll CI checks via GitLab CLI."""
        # glab mr events gives pipeline status
        cmd = ["glab", "mr", "show", pr_url.split("/")[-1], "--json", "status,pipeline"]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                logger.warning("glab mr show failed: %s", result.stderr.strip())
                return False, 0

            mr_data = json.loads(result.stdout)
            status = mr_data.get("pipeline", {}).get("status", "pending")

            # Record to telemetry
            self._store.record_ci_check(
                pr_url=pr_url,
                check_name="gitlab/ci",
                check_status=status,
                check_conclusion=status if status in ("success", "failed", "canceled") else None,
            )

            all_green = status in ("success", "running")
            total = 1

            if not all_green:
                fail_count = self._lifecycle.increment_ci_failures()
                if fail_count >= self._max_failures:
                    logger.warning("CI failure limit reached (%d/%d) for task %r",
                                   fail_count, self._max_failures, self._lifecycle.task_slug)

            return all_green, total

        except json.JSONDecodeError:
            logger.error("Invalid JSON from glab mr show")
            return False, 0
        except subprocess.TimeoutExpired:
            logger.warning("glab mr show timed out")
            return False, 0
        except Exception:
            logger.exception("Error polling CI via glab")
            return False, 0
