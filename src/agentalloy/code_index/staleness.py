"""Registry-vs-git staleness checks for indexed repos.

Import-light on purpose (subprocess only): shared by the ``agentalloy code``
CLI (status markers) and the service lifespan (one INFO line per stale repo).
Every helper is best-effort — a missing path, a non-git directory, or any git
failure reads as "not stale"/"unknown", never an exception.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

_GIT_TIMEOUT_S = 5.0


def _git(repo_path: Path, *argv: str) -> str | None:
    """Run one git command in *repo_path*; stripped stdout, or None on any failure."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_path), *argv],
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def head_commit(repo_path: Path) -> str | None:
    """Current ``HEAD`` sha of *repo_path*, or None (missing/non-git/failure)."""
    if not repo_path.is_dir():
        return None
    return _git(repo_path, "rev-parse", "HEAD")


def commits_behind(repo_path: Path, stored_sha: str) -> int | None:
    """``git rev-list --count stored..HEAD``, or None when git can't answer
    (e.g. the stored sha vanished from history after a rebase)."""
    count = _git(repo_path, "rev-list", "--count", f"{stored_sha}..HEAD")
    if count is None or not count.isdigit():
        return None
    return int(count)


@dataclass(frozen=True)
class Staleness:
    """One repo's index-vs-worktree drift verdict."""

    stale: bool
    commits_behind: int | None  # only meaningful when stale


def check_staleness(repo_path: Path, stored_sha: str | None) -> Staleness:
    """Compare the registry's ``head_sha`` against the repo's current HEAD.

    Not stale when the comparison is impossible (no stored sha, missing path,
    non-git dir) — the nudge must stay silent rather than cry wolf.
    """
    if not stored_sha:
        return Staleness(stale=False, commits_behind=None)
    current = head_commit(repo_path)
    if current is None or current == stored_sha:
        return Staleness(stale=False, commits_behind=None)
    return Staleness(stale=True, commits_behind=commits_behind(repo_path, stored_sha))
