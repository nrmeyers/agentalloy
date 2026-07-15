"""Tests for the post-checkout-hook-invoked worktree auto-wire logic.

See ``agentalloy.install.subcommands.auto_wire_worktree``: given a freshly
created, not-yet-wired linked worktree of an already-wired repo, replicates
that repo's harness/upstream/lifecycle-mode wiring into the worktree without
requiring a manual ``agentalloy worktree``/``add`` run.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from agentalloy.install.subcommands.auto_wire_worktree import (
    _main_checkout_root,
    _try_auto_wire,
    run_auto_wire_worktree,
)

_MOD = "agentalloy.install.subcommands.auto_wire_worktree"


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


class TestMainCheckoutRoot:
    def test_from_worktree_resolves_main_root(self, repo: Path, worktree: Path) -> None:
        assert _main_checkout_root(worktree) == repo

    def test_from_main_checkout_returns_none(self, repo: Path) -> None:
        assert _main_checkout_root(repo) is None

    def test_not_a_git_repo_returns_none(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        assert _main_checkout_root(plain) is None


class TestTryAutoWire:
    def test_skips_when_already_wired(self, worktree: Path) -> None:
        (worktree / ".agentalloy").mkdir()
        (worktree / ".agentalloy" / "phase").write_text("phase: build\n")
        with patch(f"{_MOD}._main_checkout_root") as mock_root:
            _try_auto_wire(worktree)
            mock_root.assert_not_called()  # short-circuited before even checking

    def test_skips_when_not_a_worktree(self, repo: Path) -> None:
        # repo itself is the main checkout — _main_checkout_root returns None.
        with patch("agentalloy.install.subcommands.add.adopt_and_wire") as mock_wire:
            _try_auto_wire(repo)
            mock_wire.assert_not_called()

    def test_skips_when_main_checkout_never_wired(self, worktree: Path) -> None:
        with patch("agentalloy.install.subcommands.add.adopt_and_wire") as mock_wire:
            _try_auto_wire(worktree)
            mock_wire.assert_not_called()

    def test_skips_when_no_harness_recorded(self, repo: Path, worktree: Path) -> None:
        (repo / ".agentalloy").mkdir()
        (repo / ".agentalloy" / "phase").write_text("phase: build\n")
        with (
            patch("agentalloy.install.state.load_state", return_value={}),
            patch("agentalloy.install.subcommands.add.adopt_and_wire") as mock_wire,
        ):
            _try_auto_wire(worktree)
            mock_wire.assert_not_called()

    def test_wires_worktree_from_main_checkout_state(self, repo: Path, worktree: Path) -> None:
        (repo / ".agentalloy").mkdir()
        (repo / ".agentalloy" / "phase").write_text("phase: build\n")
        (repo / ".agentalloy" / "upstream").write_text(
            "url: http://localhost:9999/v1\nmodel: test-model\nkey_env: MY_KEY\n"
        )
        state = {
            "harness_files_written": [
                {"harness": "hermes-agent", "repo_root": str(repo), "path": "x"},
            ]
        }
        with (
            patch("agentalloy.install.state.load_state", return_value=state),
            patch("agentalloy.install.subcommands.add.resolve_port", return_value=47950),
            patch("agentalloy.install.subcommands.add.adopt_and_wire") as mock_wire,
        ):
            _try_auto_wire(worktree)
            mock_wire.assert_called_once_with(
                "hermes-agent",
                worktree,
                port=47950,
                upstream_url="http://localhost:9999/v1",
                upstream_model="test-model",
                key_env="MY_KEY",
                lifecycle_mode="full",
            )

    def test_wires_every_harness_recorded_for_the_main_repo(
        self, repo: Path, worktree: Path
    ) -> None:
        (repo / ".agentalloy").mkdir()
        (repo / ".agentalloy" / "phase").write_text("phase: build\n")
        state = {
            "harness_files_written": [
                {"harness": "hermes-agent", "repo_root": str(repo), "path": "x"},
                {"harness": "claude-code", "repo_root": str(repo), "path": "y"},
                # A DIFFERENT repo's entry must not leak in.
                {"harness": "cline", "repo_root": "/some/other/repo", "path": "z"},
            ]
        }
        with (
            patch("agentalloy.install.state.load_state", return_value=state),
            patch("agentalloy.install.subcommands.add.resolve_port", return_value=47950),
            patch("agentalloy.install.subcommands.add.adopt_and_wire") as mock_wire,
        ):
            _try_auto_wire(worktree)
            wired_harnesses = {c.args[0] for c in mock_wire.call_args_list}
            assert wired_harnesses == {"hermes-agent", "claude-code"}


class TestRunAutoWireWorktree:
    def test_never_raises_on_internal_failure(self, worktree: Path) -> None:
        with patch(f"{_MOD}._main_checkout_root", side_effect=RuntimeError("boom")):
            assert run_auto_wire_worktree(worktree) == 0

    def test_returns_zero_on_the_common_noop_path(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        assert run_auto_wire_worktree(plain) == 0
