"""Pin AgentAlloy's code-indexer slug to codebase-indexer's canonical rule.

These assertions encode the contract that codebase-indexer's
``app/services/slug.py`` is the system of record. If codebase-indexer changes
its slug derivation, these tests should change in lockstep — a silent drift
here means AgentAlloy's ``/search`` queries start 404ing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agentalloy.code_indexer_slug import (
    derive_slug,
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
    # This is the exact string codebase-indexer stores its index under.
    assert repo_slug(repo) == "navistone__TheForge"


def test_multi_remote_falls_back_to_basename(tmp_path):
    """origin (fork) + upstream is ambiguous — both tools refuse to guess."""
    repo = tmp_path / "TheForge"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "remote", "add", "origin", "git@github.com:fork/TheForge.git")
    _git(repo, "remote", "add", "upstream", "git@github.com:navistone/TheForge.git")
    assert repo_slug(repo) == "TheForge"


def test_non_github_origin_falls_back_to_basename(tmp_path):
    repo = tmp_path / "TheForge"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "remote", "add", "origin", "https://gitlab.com/navistone/TheForge.git")
    assert repo_slug(repo) == "TheForge"


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
