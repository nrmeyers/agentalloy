"""Tests for the post-checkout auto-wire-worktree hook installer.

See ``agentalloy.install.git_hooks``: installs a small ``post-checkout`` git
hook (shared across a repo's worktrees) that shells out to
``agentalloy auto-wire-worktree`` so a freshly created git worktree gets
wired automatically instead of silently composing nothing.
"""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import pytest

from agentalloy.install.git_hooks import (
    _BEGIN,
    _END,
    install_post_checkout_hook,
    uninstall_post_checkout_hook,
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.com")
    _git(root, "config", "user.name", "t")
    (root / "f.txt").write_text("hi\n")
    _git(root, "add", "f.txt")
    _git(root, "commit", "-q", "-m", "init")
    return root


class TestInstall:
    def test_creates_executable_hook(self, repo: Path) -> None:
        hook = install_post_checkout_hook(repo)
        assert hook is not None
        assert hook == repo / ".git" / "hooks" / "post-checkout"
        assert hook.stat().st_mode & stat.S_IXUSR

    def test_content_has_shebang_and_sentinels(self, repo: Path) -> None:
        hook = install_post_checkout_hook(repo)
        text = hook.read_text()
        assert text.startswith("#!/bin/sh\n")
        assert _BEGIN in text
        assert _END in text
        assert "auto-wire-worktree" in text

    def test_idempotent(self, repo: Path) -> None:
        install_post_checkout_hook(repo)
        first = (repo / ".git" / "hooks" / "post-checkout").read_text()
        install_post_checkout_hook(repo)
        second = (repo / ".git" / "hooks" / "post-checkout").read_text()
        assert first == second
        assert first.count(_BEGIN) == 1

    def test_preserves_existing_hook_content(self, repo: Path) -> None:
        hook_path = repo / ".git" / "hooks" / "post-checkout"
        hook_path.parent.mkdir(parents=True, exist_ok=True)
        hook_path.write_text("#!/bin/sh\necho 'user hook'\n")
        install_post_checkout_hook(repo)
        text = hook_path.read_text()
        assert "echo 'user hook'" in text
        assert _BEGIN in text

    def test_not_a_git_repo_returns_none(self, tmp_path: Path) -> None:
        plain = tmp_path / "not-a-repo"
        plain.mkdir()
        assert install_post_checkout_hook(plain) is None

    def test_resolves_same_hook_from_a_linked_worktree(self, repo: Path) -> None:
        """The whole point: installing once from either side covers both."""
        wt = repo.parent / "wt1"
        _git(repo, "worktree", "add", "-q", "-b", "feature", str(wt))
        from_main = install_post_checkout_hook(repo)
        from_worktree = install_post_checkout_hook(wt)
        assert from_main is not None
        assert from_main == from_worktree


class TestUninstall:
    def test_removes_our_block_only(self, repo: Path) -> None:
        hook_path = repo / ".git" / "hooks" / "post-checkout"
        hook_path.parent.mkdir(parents=True, exist_ok=True)
        hook_path.write_text("#!/bin/sh\necho 'user hook'\n")
        install_post_checkout_hook(repo)
        uninstall_post_checkout_hook(repo)
        text = hook_path.read_text()
        assert "echo 'user hook'" in text
        assert _BEGIN not in text

    def test_deletes_file_when_our_block_was_the_only_content(self, repo: Path) -> None:
        hook_path = repo / ".git" / "hooks" / "post-checkout"
        install_post_checkout_hook(repo)
        assert hook_path.exists()
        uninstall_post_checkout_hook(repo)
        assert not hook_path.exists()

    def test_noop_when_no_hook_present(self, repo: Path) -> None:
        uninstall_post_checkout_hook(repo)  # must not raise
        assert not (repo / ".git" / "hooks" / "post-checkout").exists()

    def test_noop_when_hook_present_but_not_ours(self, repo: Path) -> None:
        hook_path = repo / ".git" / "hooks" / "post-checkout"
        hook_path.parent.mkdir(parents=True, exist_ok=True)
        hook_path.write_text("#!/bin/sh\necho 'someone else'\n")
        uninstall_post_checkout_hook(repo)
        assert "someone else" in hook_path.read_text()
