"""Worktree resolution and HEAD staleness (TF2, TF3).

TF2: Two checkouts of the same remote coexist and resolve correctly.
TF3: HEAD reporting and staleness visibility in search/bundle/status.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from agentalloy.code_index.store.jobs_store import (
    CodeIndexJobsStore,
    repo_path_key,
)
from agentalloy.code_index.store.open import code_index_paths


@pytest.fixture
def store(tmp_path: Path) -> Iterator[CodeIndexJobsStore]:
    s = CodeIndexJobsStore(tmp_path / "jobs.sqlite")
    yield s
    s.close()


# ---------------------------------------------------------------------------
# TF2 — two checkouts of the same remote coexist and resolve correctly
# ---------------------------------------------------------------------------


class TestTwoCheckoutsCoexist:
    """Multiple checkouts of the same remote get separate indexes."""

    def test_separate_data_dirs_for_same_slug(
        self, store: CodeIndexJobsStore, tmp_path: Path
    ) -> None:
        """Two checkouts of the same slug get distinct data directories."""
        path_a = str(tmp_path / "checkout_a")
        path_b = str(tmp_path / "checkout_b")

        store.upsert_repo(slug="org__repo", repo_path=path_a, data_dir="/data/a", head_sha="aaa")
        store.upsert_repo(slug="org__repo", repo_path=path_b, data_dir="/data/b", head_sha="bbb")

        repos = store.get_repos_by_slug("org__repo")
        assert len(repos) == 2
        paths = {r.repo_path for r in repos}
        assert paths == {path_a, path_b}

    def test_get_repo_slug_only_returns_none_when_ambiguous(
        self, store: CodeIndexJobsStore, tmp_path: Path
    ) -> None:
        """get_repo(slug) returns None when multiple checkouts exist."""
        store.upsert_repo(
            slug="org__repo", repo_path=str(tmp_path / "a"), data_dir="/a", head_sha="aaa"
        )
        store.upsert_repo(
            slug="org__repo", repo_path=str(tmp_path / "b"), data_dir="/b", head_sha="bbb"
        )
        assert store.get_repo("org__repo") is None

    def test_get_repo_exact_match(self, store: CodeIndexJobsStore, tmp_path: Path) -> None:
        """get_repo(slug, repo_path=...) returns the exact entry."""
        path_a = str(tmp_path / "a")
        path_b = str(tmp_path / "b")
        store.upsert_repo(slug="org__repo", repo_path=path_a, data_dir="/a", head_sha="aaa")
        store.upsert_repo(slug="org__repo", repo_path=path_b, data_dir="/b", head_sha="bbb")

        repo_a = store.get_repo("org__repo", repo_path=path_a)
        assert repo_a is not None
        assert repo_a.head_sha == "aaa"

        repo_b = store.get_repo("org__repo", repo_path=path_b)
        assert repo_b is not None
        assert repo_b.head_sha == "bbb"

    def test_get_repo_sole_entry_fallback(self, store: CodeIndexJobsStore, tmp_path: Path) -> None:
        """get_repo(slug) returns the entry when only one checkout exists."""
        store.upsert_repo(
            slug="org__repo", repo_path=str(tmp_path / "a"), data_dir="/a", head_sha="aaa"
        )
        repo = store.get_repo("org__repo")
        assert repo is not None
        assert repo.head_sha == "aaa"

    def test_resolve_repo_cwd_matches_enclosing_checkout(
        self, store: CodeIndexJobsStore, tmp_path: Path
    ) -> None:
        """resolve_repo picks the entry whose repo_path encloses cwd."""
        checkout_a = tmp_path / "checkout_a"
        checkout_b = tmp_path / "checkout_b"
        checkout_a.mkdir(parents=True)
        checkout_b.mkdir(parents=True)

        store.upsert_repo(
            slug="org__repo",
            repo_path=str(checkout_a),
            data_dir="/data/a",
            head_sha="aaa",
        )
        store.upsert_repo(
            slug="org__repo",
            repo_path=str(checkout_b),
            data_dir="/data/b",
            head_sha="bbb",
        )

        # cwd inside checkout_a → resolves to checkout_a
        sub_a = checkout_a / "src" / "deep"
        sub_a.mkdir(parents=True)
        resolved = store.resolve_repo("org__repo", cwd=str(sub_a))
        assert resolved is not None
        assert resolved.repo_path == str(checkout_a)
        assert resolved.head_sha == "aaa"

        # cwd inside checkout_b → resolves to checkout_b
        sub_b = checkout_b / "lib"
        sub_b.mkdir(parents=True)
        resolved = store.resolve_repo("org__repo", cwd=str(sub_b))
        assert resolved is not None
        assert resolved.repo_path == str(checkout_b)
        assert resolved.head_sha == "bbb"

    def test_resolve_repo_sole_entry_without_cwd(
        self, store: CodeIndexJobsStore, tmp_path: Path
    ) -> None:
        """resolve_repo falls back to sole entry even without cwd."""
        store.upsert_repo(
            slug="org__repo",
            repo_path=str(tmp_path / "a"),
            data_dir="/a",
            head_sha="aaa",
        )
        resolved = store.resolve_repo("org__repo")
        assert resolved is not None
        assert resolved.head_sha == "aaa"

    def test_resolve_repo_ambiguous_without_cwd(
        self, store: CodeIndexJobsStore, tmp_path: Path
    ) -> None:
        """resolve_repo returns None when multiple checkouts and no cwd."""
        store.upsert_repo(
            slug="org__repo", repo_path=str(tmp_path / "a"), data_dir="/a", head_sha="aaa"
        )
        store.upsert_repo(
            slug="org__repo", repo_path=str(tmp_path / "b"), data_dir="/b", head_sha="bbb"
        )
        assert store.resolve_repo("org__repo", cwd=None) is None

    def test_resolve_repo_cwd_outside_all_checkouts(
        self, store: CodeIndexJobsStore, tmp_path: Path
    ) -> None:
        """resolve_repo returns None when cwd is outside all registered checkouts (no sole-entry fallback)."""
        store.upsert_repo(
            slug="org__repo",
            repo_path=str(tmp_path / "checkout_a"),
            data_dir="/a",
            head_sha="aaa",
        )
        store.upsert_repo(
            slug="org__repo",
            repo_path=str(tmp_path / "checkout_b"),
            data_dir="/b",
            head_sha="bbb",
        )
        outside = tmp_path / "unrelated"
        outside.mkdir()
        resolved = store.resolve_repo("org__repo", cwd=str(outside))
        assert resolved is None


class TestRepoPathKey:
    """Deterministic path hashing for data directory scoping."""

    def test_deterministic(self) -> None:
        assert repo_path_key("/some/path") == repo_path_key("/some/path")

    def test_different_paths_different_keys(self) -> None:
        key_a = repo_path_key("/home/user/project")
        key_b = repo_path_key("/opt/project")
        assert key_a != key_b

    def test_hex_format(self) -> None:
        key = repo_path_key("/test")
        assert len(key) == 8
        int(key, 16)  # valid hex


class TestCodeIndexPathsScoping:
    """Data directories are scoped per-checkout when repo_path is provided."""

    def test_separate_dirs_with_repo_path(self) -> None:
        paths_a = code_index_paths(
            None,
            "org__repo",
            repo_path="/home/user/project",
        )
        paths_b = code_index_paths(
            None,
            "org__repo",
            repo_path="/opt/project",
        )
        # Different repo_paths → different data dirs
        assert paths_a.repo_dir != paths_b.repo_dir
        # Each contains the path key
        key_a = repo_path_key("/home/user/project")
        key_b = repo_path_key("/opt/project")
        assert key_a in str(paths_a.repo_dir)
        assert key_b in str(paths_b.repo_dir)

    def test_fallback_without_repo_path(self) -> None:
        paths = code_index_paths(None, "org__repo")
        assert paths.repo_key == "default"
        # Legacy layout: repos/{slug}/
        assert paths.repo_dir.name == "org__repo"


# ---------------------------------------------------------------------------
# TF3 — HEAD reporting and staleness visibility
# ---------------------------------------------------------------------------


class TestHeadStalenessReporting:
    """HEAD tracking and staleness detection in the registry."""

    def test_head_recorded_on_index(self, store: CodeIndexJobsStore, tmp_path: Path) -> None:
        """head_sha is recorded when upsert_repo is called with it."""
        repo = tmp_path / "repo"
        repo.mkdir()
        store.upsert_repo(
            slug="org__repo",
            repo_path=str(repo),
            data_dir="/data/repo",
            head_sha="abc123def456",
        )
        indexed = store.get_repo("org__repo")
        assert indexed is not None
        assert indexed.head_sha == "abc123def456"

    def test_head_updated_on_mark_indexed(self, store: CodeIndexJobsStore, tmp_path: Path) -> None:
        """mark_indexed advances head_sha to the new value."""
        store.upsert_repo(
            slug="org__repo",
            repo_path=str(tmp_path / "repo"),
            data_dir="/data/repo",
            head_sha="old_head",
        )
        store.mark_indexed("org__repo", head_sha="new_head")
        indexed = store.get_repo("org__repo")
        assert indexed is not None
        assert indexed.head_sha == "new_head"

    def test_head_preserved_when_not_provided(
        self, store: CodeIndexJobsStore, tmp_path: Path
    ) -> None:
        """mark_indexed without head_sha preserves the existing value."""
        store.upsert_repo(
            slug="org__repo",
            repo_path=str(tmp_path / "repo"),
            data_dir="/data/repo",
            head_sha="original",
        )
        store.mark_indexed("org__repo")  # no head_sha
        indexed = store.get_repo("org__repo")
        assert indexed is not None
        assert indexed.head_sha == "original"

    def test_staleness_detectable_from_registry(
        self, store: CodeIndexJobsStore, tmp_path: Path
    ) -> None:
        """Staleness is detectable by comparing indexed head vs current head."""
        repo = tmp_path / "repo"
        repo.mkdir()

        # Initialize a real git repo
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=repo,
            check=True,
            capture_output=True,
        )

        # First commit
        (repo / "a.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        first_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        # Register the repo at first commit
        store.upsert_repo(
            slug="org__repo",
            repo_path=str(repo),
            data_dir="/data/repo",
            head_sha=first_sha,
        )

        # Second commit (HEAD moves)
        (repo / "b.py").write_text("y = 2\n")
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "second"],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        second_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        # Verify: indexed head != current head → stale
        indexed = store.get_repo("org__repo")
        assert indexed is not None
        assert indexed.head_sha == first_sha
        assert first_sha != second_sha  # different commits

        # The RepoView.is_stale logic:
        is_stale = (
            second_sha is not None
            and indexed.head_sha is not None
            and second_sha != indexed.head_sha
        )
        assert is_stale is True

    def test_not_stale_when_heads_match(self, store: CodeIndexJobsStore, tmp_path: Path) -> None:
        """No staleness when indexed head matches current head."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        (repo / "a.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        store.upsert_repo(
            slug="org__repo",
            repo_path=str(repo),
            data_dir="/data/repo",
            head_sha=sha,
        )

        indexed = store.get_repo("org__repo")
        assert indexed is not None
        is_stale = sha != indexed.head_sha
        assert is_stale is False


class TestMultipleCheckoutsIndependentHeads:
    """Each checkout tracks its own HEAD independently."""

    def test_independent_head_per_checkout(self, store: CodeIndexJobsStore, tmp_path: Path) -> None:
        """Two checkouts of the same slug track separate HEAD values."""
        checkout_a = tmp_path / "checkout_a"
        checkout_b = tmp_path / "checkout_b"
        checkout_a.mkdir()
        checkout_b.mkdir()

        # Initialize both as git repos
        for checkout in (checkout_a, checkout_b):
            subprocess.run(["git", "init"], cwd=checkout, check=True, capture_output=True)
            subprocess.run(
                ["git", "config", "user.email", "test@test.com"],
                cwd=checkout,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test"],
                cwd=checkout,
                check=True,
                capture_output=True,
            )
            (checkout / "a.py").write_text("x = 1\n")
            subprocess.run(["git", "add", "."], cwd=checkout, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", "initial"],
                cwd=checkout,
                check=True,
                capture_output=True,
            )

        sha_a = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=checkout_a,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        sha_b = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=checkout_b,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        # Register both checkouts
        store.upsert_repo(
            slug="org__repo",
            repo_path=str(checkout_a),
            data_dir="/data/a",
            head_sha=sha_a,
        )
        store.upsert_repo(
            slug="org__repo",
            repo_path=str(checkout_b),
            data_dir="/data/b",
            head_sha=sha_b,
        )

        # Each tracks its own HEAD
        repo_a = store.get_repo("org__repo", repo_path=str(checkout_a))
        repo_b = store.get_repo("org__repo", repo_path=str(checkout_b))
        assert repo_a is not None and repo_a.head_sha == sha_a
        assert repo_b is not None and repo_b.head_sha == sha_b

        # Move HEAD in checkout_a only
        (checkout_a / "b.py").write_text("y = 2\n")
        subprocess.run(["git", "add", "."], cwd=checkout_a, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "second"],
            cwd=checkout_a,
            check=True,
            capture_output=True,
        )
        new_sha_a = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=checkout_a,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        # checkout_a is stale, checkout_b is fresh
        is_stale_a = new_sha_a != repo_a.head_sha
        is_stale_b = sha_b != repo_b.head_sha
        assert is_stale_a is True
        assert is_stale_b is False
