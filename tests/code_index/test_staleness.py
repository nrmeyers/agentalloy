"""code_index.staleness — registry-vs-git drift checks + the startup nudge."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

from agentalloy.code_index.staleness import check_staleness, commits_behind, head_commit


def _git(repo: Path, *argv: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *argv], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def make_git_repo(root: Path) -> str:
    """git init + one commit; returns the HEAD sha."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "a.py").write_text("x = 1\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "one")
    return _git(root, "rev-parse", "HEAD")


def advance_head(root: Path, name: str = "b.py") -> str:
    (root / name).write_text("y = 2\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", f"add {name}")
    return _git(root, "rev-parse", "HEAD")


def test_head_commit_non_git_and_missing(tmp_path: Path) -> None:
    assert head_commit(tmp_path / "missing") is None
    plain = tmp_path / "plain"
    plain.mkdir()
    assert head_commit(plain) is None


def test_check_staleness_fresh_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    sha = make_git_repo(repo)
    verdict = check_staleness(repo, sha)
    assert verdict.stale is False


def test_check_staleness_moved_head_counts_commits(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    first = make_git_repo(repo)
    advance_head(repo, "b.py")
    advance_head(repo, "c.py")
    verdict = check_staleness(repo, first)
    assert verdict.stale is True
    assert verdict.commits_behind == 2


def test_check_staleness_rebased_away_sha_falls_back_to_plain_stale(tmp_path: Path) -> None:
    """A stored sha no longer in history (rebase) → stale, but no count."""
    repo = tmp_path / "repo"
    make_git_repo(repo)
    gone = "0" * 40  # never a real object in this repo
    verdict = check_staleness(repo, gone)
    assert verdict.stale is True
    assert verdict.commits_behind is None
    assert commits_behind(repo, gone) is None


def test_check_staleness_silent_without_comparison(tmp_path: Path) -> None:
    # No stored sha, missing path, non-git dir: all read as not stale.
    assert check_staleness(tmp_path / "gone", "abc").stale is False
    assert check_staleness(tmp_path, None).stale is False
    assert check_staleness(tmp_path, "").stale is False


def test_log_stale_repos_one_info_line_per_stale_repo(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from agentalloy.code_index.api.state import CodeIndexState
    from agentalloy.code_index.store import open_jobs
    from agentalloy.config import Settings

    from .conftest import FakeEmbedClient

    settings = Settings(code_index_data_dir=str(tmp_path / "ci-data"))
    jobs = open_jobs(settings)
    try:
        stale_repo = tmp_path / "stale"
        first = make_git_repo(stale_repo)
        advance_head(stale_repo)
        jobs.upsert_repo(slug="stale", repo_path=str(stale_repo), data_dir="/d/s", head_sha=first)

        fresh_repo = tmp_path / "fresh"
        sha = make_git_repo(fresh_repo)
        jobs.upsert_repo(slug="fresh", repo_path=str(fresh_repo), data_dir="/d/f", head_sha=sha)

        # Missing path: comparison impossible → silent, and startup survives.
        jobs.upsert_repo(slug="gone", repo_path=str(tmp_path / "gone"), data_dir="/d/g")

        state = CodeIndexState(settings=settings, embed_client=FakeEmbedClient(), jobs=jobs)
        with caplog.at_level(logging.INFO, logger="agentalloy.code_index.api.state"):
            state.log_stale_repos()
    finally:
        jobs.close()

    stale_lines = [r for r in caplog.records if "stale" in r.getMessage()]
    assert len(stale_lines) == 1
    msg = stale_lines[0].getMessage()
    assert "stale" in msg and "1 commits behind" in msg
    assert "agentalloy code index" in msg
