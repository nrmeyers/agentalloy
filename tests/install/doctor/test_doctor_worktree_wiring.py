"""Doctor check: an unwired linked worktree (#549).

A worktree created before the post-checkout auto-wire hook was installed (or
one where the hook fired and soft-failed) is silently left with no
``.agentalloy/`` of its own — it composes nothing and a Claude Code session
started there inherits ``ANTHROPIC_BASE_URL`` from whatever shell env it was
launched in, typically the main checkout's token. ``_check_worktree_wiring``
gives that state a diagnostic instead of silence.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from agentalloy.install.subcommands.doctor import _check_worktree_wiring

_MOD = "agentalloy.install.subcommands.doctor"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "main"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.com")
    _git(root, "config", "user.name", "t")
    (root / "f.txt").write_text("hi\n")
    _git(root, "add", "f.txt")
    _git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def worktree(repo: Path) -> Path:
    wt = repo.parent / "wt1"
    _git(repo, "worktree", "add", "-q", "-b", "feature", str(wt))
    return wt


def _run(cwd: Path) -> dict:
    with patch(f"{_MOD}.Path.cwd", return_value=cwd):
        return _check_worktree_wiring()


class TestCheckWorktreeWiring:
    def test_not_a_git_repo_is_silent(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        check = _run(plain)
        assert check["passed"] is True
        assert "severity" not in check

    def test_main_checkout_is_silent(self, repo: Path) -> None:
        check = _run(repo)
        assert check["passed"] is True
        assert "severity" not in check
        assert check["detail"] == "not a linked worktree"

    def test_unwired_main_checkout_is_silent(self, repo: Path, worktree: Path) -> None:
        """Nothing to auto-wire from -- the main checkout was never wired either."""
        check = _run(worktree)
        assert check["passed"] is True
        assert "severity" not in check
        assert check["detail"] == "main checkout is not wired"

    def test_wired_worktree_is_silent(self, repo: Path, worktree: Path) -> None:
        (repo / ".agentalloy").mkdir()
        (worktree / ".agentalloy").mkdir()
        check = _run(worktree)
        assert check["passed"] is True
        assert "severity" not in check

    def test_unwired_worktree_of_wired_repo_warns(self, repo: Path, worktree: Path) -> None:
        (repo / ".agentalloy").mkdir()
        check = _run(worktree)
        assert check["passed"] is True
        assert check["severity"] == "warn"
        assert str(repo) in check["detail"]
        assert "auto-wire-worktree" in check["remediation"]

    # Regression: `_main_checkout_root` resolves via `--git-common-dir`, which
    # is the SAME path from every directory in the repo -- comparing it
    # directly against an arbitrary cwd (rather than cwd's own toplevel) false
    # -positives "this is an unwired worktree of itself" from any subdirectory
    # of the main checkout, and the remediation it points at would then write
    # a nested .agentalloy/ into that subdirectory.

    def test_subdirectory_of_main_checkout_is_silent(self, repo: Path) -> None:
        sub = repo / "src"
        sub.mkdir()
        check = _run(sub)
        assert check["passed"] is True
        assert "severity" not in check
        assert check["detail"] == "not a linked worktree"

    def test_subdirectory_of_wired_worktree_is_silent(self, repo: Path, worktree: Path) -> None:
        (repo / ".agentalloy").mkdir()
        (worktree / ".agentalloy").mkdir()
        sub = worktree / "src"
        sub.mkdir()
        check = _run(sub)
        assert check["passed"] is True
        assert "severity" not in check

    def test_subdirectory_of_unwired_worktree_still_warns(self, repo: Path, worktree: Path) -> None:
        (repo / ".agentalloy").mkdir()
        sub = worktree / "src"
        sub.mkdir()
        check = _run(sub)
        assert check["passed"] is True
        assert check["severity"] == "warn"
        assert str(repo) in check["detail"]
        assert str(worktree) in check["detail"]
