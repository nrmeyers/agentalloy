"""Repo registry tests — persistent multi-repo index membership."""

import json
from pathlib import Path

from agentalloy.registry import load_repos, registry_path, save_repos, upsert_repo


def test_load_repos_missing_file() -> None:
    state_duck = Path("/tmp/aa-registry-test") / "state.duck"
    assert load_repos(state_duck) == []


def test_load_repos_corrupt_file() -> None:
    duck = Path("/tmp/aa-registry-test-c") / "state.duck"
    duck.parent.mkdir(parents=True, exist_ok=True)
    registry_path(duck).write_text("not json at all")
    assert load_repos(duck) == []


def test_load_repos_non_list() -> None:
    duck = Path("/tmp/aa-registry-test-n") / "state.duck"
    duck.parent.mkdir(parents=True, exist_ok=True)
    registry_path(duck).write_text(json.dumps({"repos": []}))
    assert load_repos(duck) == []


def test_upsert_repo_canonicalizes_path(tmp_path: Path) -> None:
    duck = tmp_path / "state.duck"
    repo = tmp_path / "some-repo"
    repo.mkdir()

    repos = upsert_repo(duck, str(repo))

    assert repos == [str(repo.resolve())]
    assert load_repos(duck) == repos
    assert (tmp_path / "repos.json").exists()


def test_upsert_repo_idempotent_and_order_preserving(tmp_path: Path) -> None:
    duck = tmp_path / "state.duck"
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()

    upsert_repo(duck, str(a))
    upsert_repo(duck, str(b))
    upsert_repo(duck, str(a))  # re-add: no duplicate, no reorder

    assert load_repos(duck) == [str(a.resolve()), str(b.resolve())]


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    duck = tmp_path / "state.duck"
    save_repos(duck, ["/x/one", "/x/two"])
    assert load_repos(duck) == ["/x/one", "/x/two"]
