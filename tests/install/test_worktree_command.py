"""``agentalloy worktree`` — git-worktree creation + per-worktree wiring isolation.

These use a *real* git repo + ``git worktree add`` (not mocked) because the whole
point is the worktree's ``.git``-is-a-file layout, which mocks would paper over.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest
import yaml

from agentalloy.api.proxy_context import (
    decode_proj_token,
    encode_proj_token,
    read_phase,
    read_upstream,
)
from agentalloy.install.subcommands import worktree
from agentalloy.install.subcommands.wire import _git_exclude_agentalloy, _resolve_git_exclude


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    )


def _init_repo(path: Path) -> None:
    """Init a git repo with one commit so HEAD exists (worktree add needs it)."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("seed\n")
    _git(path, "add", "README.md")
    _git(path, "commit", "-q", "-m", "init")


def _global_hermes_config(home: Path, base_url: str = "http://10.0.0.9:60000/v1") -> None:
    (home / ".hermes").mkdir(parents=True, exist_ok=True)
    (home / ".hermes" / "config.yaml").write_text(
        f"model:\n  provider: custom\n  base_url: {base_url}\n  default: qwen3.6\n"
    )


def _args(**over: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "harness": "hermes-agent",
        "branch": "feat-x",
        "path": None,
        "new_branch": True,
        "port": 47950,
        "upstream_url": None,
        "upstream_model": None,
        "key_env": None,
    }
    base.update(over)
    return argparse.Namespace(**base)


class TestWorktreeRun:
    def test_creates_worktree_and_wires_isolated_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        _global_hermes_config(home)
        monkeypatch.setattr(Path, "home", lambda: home)

        repo = tmp_path / "repo"
        _init_repo(repo)
        wt = tmp_path / "wt-feat-x"
        monkeypatch.chdir(repo)

        rc = worktree._run(_args(path=str(wt)))
        assert rc == 0

        # The worktree exists and is a real linked work tree (.git is a FILE).
        assert wt.is_dir()
        assert (wt / ".git").is_file()

        # Upstream adopted into the WORKTREE's .agentalloy/upstream.
        up = read_upstream(wt)
        assert up is not None and up.url == "http://10.0.0.9:60000/v1"

        # Interception wired with the worktree's own /proj token — distinct from
        # the main checkout's, which is what keeps the two sessions isolated.
        cfg = yaml.safe_load((wt / ".hermes" / "config.yaml").read_text())
        base_url = cfg["model"]["base_url"]
        assert base_url.startswith("http://localhost:47950/proj/")
        token = base_url.split("/proj/")[1].split("/")[0]
        assert decode_proj_token(token) == wt.resolve()
        assert token != encode_proj_token(repo.resolve())

        # Phase seeded in the worktree (it's activated, independent of main).
        assert read_phase(wt) is not None

    def test_not_in_repo_errors(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        monkeypatch.chdir(tmp_path)  # no git repo here
        assert worktree._run(_args(path=str(tmp_path / "wt"))) == 1

    def test_existing_path_errors(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        repo = tmp_path / "repo"
        _init_repo(repo)
        taken = tmp_path / "taken"
        taken.mkdir()
        monkeypatch.chdir(repo)
        assert worktree._run(_args(path=str(taken))) == 1

    def test_unknown_harness_errors(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        monkeypatch.chdir(repo)
        assert worktree._run(_args(harness="nope", path=str(tmp_path / "wt"))) == 1


class TestGitExcludeInWorktree:
    """Regression for the bug where `.git`-is-a-file made the exclude a silent no-op."""

    def test_excludes_agentalloy_in_worktree(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        wt = tmp_path / "wt"
        _git(repo, "worktree", "add", "-b", "b1", str(wt))
        assert (wt / ".git").is_file()  # precondition: real worktree layout

        _git_exclude_agentalloy(wt)

        exclude = _resolve_git_exclude(wt)
        assert exclude is not None
        assert ".agentalloy/" in exclude.read_text().splitlines()

    def test_excludes_agentalloy_in_normal_checkout(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        _git_exclude_agentalloy(repo)
        assert ".agentalloy/" in (repo / ".git" / "info" / "exclude").read_text().splitlines()

    def test_noop_outside_git_repo(self, tmp_path: Path) -> None:
        # Must not raise and must resolve to no exclude file.
        _git_exclude_agentalloy(tmp_path)
        assert _resolve_git_exclude(tmp_path) is None
