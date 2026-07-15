"""Pin the code-index repo-slug derivation rule.

The slug is the key every ``/code/*`` index and search lookup resolves by:
single github.com origin → ``{org}__{repo}``; single origin on any other
parseable host (GitLab, Bitbucket, self-hosted) → ``{host}__{org}__{repo}``;
otherwise the slugified directory basename. These tests freeze that rule — a
silent change strands existing per-repo indexes under keys nothing queries
anymore.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agentalloy.code_index.slug import (
    derive_slug,
    parse_git_remote,
    parse_github_remote,
    repo_slug,
    slugify_repo,
)


@pytest.mark.parametrize(
    "url,expected",
    [
        ("git@github.com:navistone/TheForge.git", ("navistone", "TheForge")),
        ("https://github.com/navistone/TheForge", ("navistone", "TheForge")),
        ("https://github.com/navistone/TheForge.git", ("navistone", "TheForge")),
        ("ssh://git@github.com/navistone/TheForge.git", ("navistone", "TheForge")),
        ("https://github.com/navistone/TheForge/", ("navistone", "TheForge")),
        # Non-GitHub hosts → None (caller falls back to basename).
        ("https://gitlab.com/foo/bar.git", None),
        ("git@gitlab.com:foo/bar.git", None),
        ("https://bitbucket.org/foo/bar", None),
        ("", None),
    ],
)
def test_parse_github_remote(url, expected):
    assert parse_github_remote(url) == expected


def test_parse_github_remote_rejects_non_str():
    assert parse_github_remote(None) is None  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "url,expected",
    [
        ("git@github.com:navistone/TheForge.git", ("github.com", "navistone", "TheForge")),
        ("https://github.com/navistone/TheForge", ("github.com", "navistone", "TheForge")),
        ("https://gitlab.com/foo/bar.git", ("gitlab.com", "foo", "bar")),
        ("git@gitlab.com:foo/bar.git", ("gitlab.com", "foo", "bar")),
        (
            "https://gitlab.com/team/backend/repo.git",
            ("gitlab.com", "team/backend", "repo"),
        ),
        ("https://bitbucket.org/foo/bar", ("bitbucket.org", "foo", "bar")),
        ("ssh://git@git.example.com/foo/bar.git", ("git.example.com", "foo", "bar")),
        # A bare repo path with no org segment is ambiguous — refuse to guess.
        ("https://gitlab.com/repo.git", None),
        ("", None),
    ],
)
def test_parse_git_remote(url, expected):
    assert parse_git_remote(url) == expected


def test_parse_git_remote_rejects_non_str():
    assert parse_git_remote(None) is None  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "name,expected",
    [
        ("navistone__TheForge", "navistone__TheForge"),
        ("weird/name@v2", "weird_name_v2"),
        ("--leading.trailing--", "leading.trailing"),
        ("", "repo"),
        ("///", "repo"),
    ],
)
def test_slugify_repo(name, expected):
    assert slugify_repo(name) == expected


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def test_single_github_origin_yields_org_repo(tmp_path):
    repo = tmp_path / "TheForge"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "remote", "add", "origin", "git@github.com:navistone/TheForge.git")
    # This is the exact string the code-index module stores its index under.
    assert repo_slug(repo) == "navistone__TheForge"


def test_multi_remote_falls_back_to_basename(tmp_path):
    """origin (fork) + upstream is ambiguous — both tools refuse to guess."""
    repo = tmp_path / "TheForge"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "remote", "add", "origin", "git@github.com:fork/TheForge.git")
    _git(repo, "remote", "add", "upstream", "git@github.com:navistone/TheForge.git")
    assert repo_slug(repo) == "TheForge"


def test_non_github_origin_yields_host_qualified_slug(tmp_path):
    repo = tmp_path / "TheForge"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "remote", "add", "origin", "https://gitlab.com/navistone/TheForge.git")
    assert repo_slug(repo) == "gitlab.com__navistone__TheForge"


def test_non_github_origin_slug_is_worktree_path_independent(tmp_path):
    """The whole point: two differently-named directories (e.g. a repo and one
    of its git worktrees) with the same GitLab origin must share a slug, so a
    worktree reuses the existing code index instead of re-indexing from scratch."""
    main = tmp_path / "myrepo"
    main.mkdir()
    _git(main, "init", "-q")
    _git(main, "remote", "add", "origin", "git@gitlab.com:navistone/TheForge.git")

    worktree = tmp_path / "myrepo-feature-branch"
    worktree.mkdir()
    _git(worktree, "init", "-q")
    _git(worktree, "remote", "add", "origin", "git@gitlab.com:navistone/TheForge.git")

    assert repo_slug(main) == repo_slug(worktree)
    assert repo_slug(main) == "gitlab.com__navistone__TheForge"


def test_different_hosts_same_org_repo_do_not_collide(tmp_path):
    gh = tmp_path / "on-github"
    gh.mkdir()
    _git(gh, "init", "-q")
    _git(gh, "remote", "add", "origin", "git@github.com:acme/widgets.git")

    gl = tmp_path / "on-gitlab"
    gl.mkdir()
    _git(gl, "init", "-q")
    _git(gl, "remote", "add", "origin", "git@gitlab.com:acme/widgets.git")

    assert repo_slug(gh) != repo_slug(gl)
    assert repo_slug(gh) == "acme__widgets"
    assert repo_slug(gl) == "gitlab.com__acme__widgets"


def test_no_remote_falls_back_to_basename(tmp_path):
    repo = tmp_path / "Some.Repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    assert repo_slug(repo) == "Some.Repo"


def test_not_a_git_dir_falls_back_to_basename(tmp_path):
    repo = tmp_path / "plain-dir"
    repo.mkdir()
    assert repo_slug(repo) == "plain-dir"


def test_derive_slug_nonexistent_path_uses_fallback():
    assert derive_slug(Path("/no/such/path/xyz"), "My-Repo") == "My-Repo"
