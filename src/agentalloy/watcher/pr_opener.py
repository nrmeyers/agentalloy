"""PR creation via VCS forge (gh/glab).

Detects which VCS is available and creates PRs using the appropriate CLI.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class PRManager:
    """Creates PRs via GitHub CLI (gh) or GitLab CLI (glab)."""

    def __init__(self, store: Any, lifecycle: Any, project_root: Path | None = None) -> None:
        self._store = store
        self._lifecycle = lifecycle
        self._project_root = project_root or Path.cwd()
        self._vcs_type: str | None = None  # "gh" or "glab"

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
        logger.warning("Neither 'gh' nor 'glab' found on PATH")
        return None

    def open_pr(
        self,
        task_slug: str,
        branch: str,
        base_branch: str,
        title: str,
        body: str,
        auto_merge: bool = False,
    ) -> str | None:
        """Open a PR and return the PR URL, or None on failure."""
        vcs = self.vcs_type
        if vcs is None:
            logger.error("No VCS CLI available (gh or glab)")
            return None

        if vcs == "gh":
            return self._open_gh_pr(branch, base_branch, title, body, auto_merge)
        else:
            return self._open_glab_pr(branch, base_branch, title, body, auto_merge)

    def _open_gh_pr(
        self,
        branch: str,
        base_branch: str,
        title: str,
        body: str,
        auto_merge: bool = False,
    ) -> str | None:
        """Open a PR via GitHub CLI."""
        cmd = [
            "gh",
            "pr",
            "create",
            "--head",
            branch,
            "--base",
            base_branch,
            "--title",
            title,
            "--body",
            body,
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(self._project_root),
            )
            if result.returncode != 0:
                logger.error("gh pr create failed: %s", result.stderr.strip())
                return None

            # gh pr create outputs the PR URL on success
            pr_url = result.stdout.strip().split("\n")[0]
            logger.info("PR created via gh: %s", pr_url)

            # Enable auto-merge if requested
            if auto_merge:
                self._gh_enable_auto_merge(pr_url)

            # Record PR URL in lifecycle
            self._lifecycle.update_pr_url(pr_url)
            return pr_url

        except subprocess.TimeoutExpired:
            logger.error("gh pr create timed out")
            return None
        except Exception:
            logger.exception("Error creating PR via gh")
            return None

    def _gh_enable_auto_merge(self, pr_url: str) -> None:
        """Enable auto-merge on a GitHub PR."""
        cmd = ["gh", "pr", "merge", "--auto", pr_url]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(self._project_root),
            )
            if result.returncode != 0:
                logger.warning("gh pr merge --auto failed: %s", result.stderr.strip())
        except Exception:
            logger.exception("Error enabling auto-merge via gh")

    def _open_glab_pr(
        self,
        branch: str,
        base_branch: str,
        title: str,
        body: str,
        auto_merge: bool = False,
    ) -> str | None:
        """Open a MR via GitLab CLI."""
        cmd = [
            "glab",
            "mr",
            "create",
            "--source-branch",
            branch,
            "--target-branch",
            base_branch,
            "--title",
            title,
            "--description",
            body,
        ]

        if auto_merge:
            cmd.append("--target-project-id")
            # glab auto-merge is handled differently
            pass

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(self._project_root),
            )
            if result.returncode != 0:
                logger.error("glab mr create failed: %s", result.stderr.strip())
                return None

            # glab mr create outputs the MR URL on success
            pr_url = result.stdout.strip().split("\n")[0]
            logger.info("MR created via glab: %s", pr_url)

            # Enable auto-merge if requested
            if auto_merge:
                self._glab_enable_auto_merge(pr_url)

            self._lifecycle.update_pr_url(pr_url)
            return pr_url

        except subprocess.TimeoutExpired:
            logger.error("glab mr create timed out")
            return None
        except Exception:
            logger.exception("Error creating MR via glab")
            return None

    def _glab_enable_auto_merge(self, mr_url: str) -> None:
        """Enable auto-merge on a GitLab MR."""
        cmd = ["glab", "mr", "merge", "--auto", mr_url]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(self._project_root),
            )
            if result.returncode != 0:
                logger.warning("glab mr merge --auto failed: %s", result.stderr.strip())
        except Exception:
            logger.exception("Error enabling auto-merge via glab")
