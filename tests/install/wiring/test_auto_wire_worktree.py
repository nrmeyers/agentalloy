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
        (worktree / ".agentalloy" / "config").write_text("lifecycle_mode: full\n")
        with patch(f"{_MOD}._main_checkout_root") as mock_root:
            _try_auto_wire(worktree)
            mock_root.assert_not_called()  # short-circuited before even checking

    def test_skips_when_wired_harness_had_no_upstream_to_adopt(self, worktree: Path) -> None:
        """A wired worktree with no ``upstream`` file must still short-circuit.

        ``adopt_upstream`` writes nothing when a harness has nothing to adopt —
        claude-code forwards the caller's own key — so ``upstream`` is absent on
        exactly the most common wiring. Keying the guard off it re-wires such a
        worktree on every post-checkout hook fire.
        """
        (worktree / ".agentalloy").mkdir()
        (worktree / ".agentalloy" / "config").write_text("lifecycle_mode: full\n")
        assert not (worktree / ".agentalloy" / "upstream").exists()
        with patch("agentalloy.install.subcommands.add.adopt_and_wire") as mock_wire:
            _try_auto_wire(worktree)
            mock_wire.assert_not_called()

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
        (repo / ".agentalloy" / "upstream").write_text(
            "url: http://localhost:47950/v1\nmodel: test\n"
        )
        with (
            patch("agentalloy.install.state.load_state", return_value={}),
            patch("agentalloy.install.subcommands.add.adopt_and_wire") as mock_wire,
        ):
            _try_auto_wire(worktree)
            mock_wire.assert_not_called()

    def test_wires_worktree_from_main_checkout_state(self, repo: Path, worktree: Path) -> None:
        (repo / ".agentalloy").mkdir()
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
        (repo / ".agentalloy" / "upstream").write_text(
            "url: http://localhost:47950/v1\nmodel: test\n"
        )
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


class TestCliBootstrapAutoWire:
    """A2: the CLI bootstrap self-heals an unwired worktree.

    The post-checkout hook only fires for worktrees created via ``git worktree
    add``; a worktree created by an agent harness skips it and stays unwired, so
    it has no repo-local wiring or token of its own and context banners key off
    the main checkout (anomalies A1/A2). The fix runs ``run_auto_wire_worktree``
    from ``main()`` on every bootstrap; it is soft-fail and marker-gated, so the
    common case is a cheap no-op.
    """

    def test_main_invokes_auto_wire_with_cwd(self, worktree: Path, monkeypatch) -> None:
        import agentalloy.install.__main__ as install_main

        monkeypatch.chdir(worktree)
        expected = Path.cwd()
        with patch.object(install_main, "auto_wire_worktree") as mock_aw:
            rc = install_main.main([])
        # Bare `agentalloy` (no subcommand) exits EXIT_USER but still self-heals.
        assert rc == install_main.EXIT_USER
        mock_aw.run_auto_wire_worktree.assert_called_once_with(expected)

    def test_main_self_heals_unwired_worktree(
        self, repo: Path, worktree: Path, monkeypatch
    ) -> None:
        """Running the CLI from an unwired worktree wires it (AC2)."""
        import agentalloy.install.__main__ as install_main

        (repo / ".agentalloy").mkdir()
        (repo / ".agentalloy" / "upstream").write_text(
            "url: http://localhost:9999/v1\nmodel: test-model\nkey_env: MY_KEY\n"
        )
        state = {
            "harness_files_written": [
                {"harness": "hermes-agent", "repo_root": str(repo), "path": "x"},
            ]
        }

        def fake_adopt_and_wire(harness, root, **kw):
            cfg = root / ".agentalloy" / "config"
            cfg.parent.mkdir(parents=True, exist_ok=True)
            cfg.write_text("lifecycle_mode: full\n")
            return None, {"ok": True}

        monkeypatch.chdir(worktree)
        with (
            patch("agentalloy.install.state.load_state", return_value=state),
            patch("agentalloy.install.subcommands.add.resolve_port", return_value=47950),
            patch(
                "agentalloy.install.subcommands.add.adopt_and_wire",
                side_effect=fake_adopt_and_wire,
            ),
        ):
            install_main.main([])
        assert (worktree / ".agentalloy" / "config").exists()

    def test_main_auto_wire_is_idempotent(
        self, repo: Path, worktree: Path, monkeypatch
    ) -> None:
        """A second bootstrap is a no-op: the marker short-circuits (AC3)."""
        import agentalloy.install.__main__ as install_main

        (repo / ".agentalloy").mkdir()
        (repo / ".agentalloy" / "upstream").write_text(
            "url: http://localhost:9999/v1\nmodel: m\n"
        )
        state = {
            "harness_files_written": [
                {"harness": "hermes-agent", "repo_root": str(repo), "path": "x"},
            ]
        }

        def fake_adopt_and_wire(harness, root, **kw):
            cfg = root / ".agentalloy" / "config"
            cfg.parent.mkdir(parents=True, exist_ok=True)
            cfg.write_text("lifecycle_mode: full\n")
            return None, {"ok": True}

        monkeypatch.chdir(worktree)
        with (
            patch("agentalloy.install.state.load_state", return_value=state),
            patch("agentalloy.install.subcommands.add.resolve_port", return_value=47950),
            patch(
                "agentalloy.install.subcommands.add.adopt_and_wire",
                side_effect=fake_adopt_and_wire,
            ) as mock_wire,
        ):
            install_main.main([])
            install_main.main([])
        # Wired on the first bootstrap; the marker makes the second a no-op.
        assert mock_wire.call_count == 1
        assert (worktree / ".agentalloy" / "config").exists()

    def test_main_leaves_a_non_worktree_checkout_alone(self, repo: Path, monkeypatch) -> None:
        """The main checkout (not a worktree) is never auto-wired (AC3)."""
        import agentalloy.install.__main__ as install_main

        (repo / ".agentalloy").mkdir()
        monkeypatch.chdir(repo)
        with patch(
            "agentalloy.install.subcommands.add.adopt_and_wire"
        ) as mock_wire:
            install_main.main([])
        mock_wire.assert_not_called()
