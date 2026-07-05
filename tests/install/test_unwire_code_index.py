# pyright: reportPrivateUsage=false
"""``unwire`` × code-index: prompt to drop the repo's index (default KEEP)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

from agentalloy.code_index.slug import repo_slug
from agentalloy.code_index.store.jobs_store import CodeIndexJobsStore
from agentalloy.install.subcommands import unwire


@pytest.fixture
def ci_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, str]:
    """(repo_root, data_dir, slug) with CODE_INDEX_DATA_DIR pointed at tmp."""
    data_root = tmp_path / "ci-data"
    monkeypatch.setenv("CODE_INDEX_DATA_DIR", str(data_root))
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo, data_root, repo_slug(repo)


def _register(data_root: Path, slug: str, repo: Path, *, active_job: bool = False) -> Path:
    """Seed the registry (and optionally an active job); returns the slug dir."""
    slug_dir = data_root / "repos" / slug
    slug_dir.mkdir(parents=True, exist_ok=True)
    (slug_dir / "graph.duck").write_text("stub")
    store = CodeIndexJobsStore(data_root / "jobs.sqlite")
    try:
        store.upsert_repo(slug=slug, repo_path=str(repo), data_dir=str(slug_dir))
        if active_job:
            store.create_job(slug=slug, repo_path=str(repo))
    finally:
        store.close()
    return slug_dir


def _registry_has(data_root: Path, slug: str) -> bool:
    store = CodeIndexJobsStore(data_root / "jobs.sqlite")
    try:
        return store.get_repo(slug) is not None
    finally:
        store.close()


class TestParser:
    def test_flags_parse(self) -> None:
        parser = argparse.ArgumentParser(prog="agentalloy")
        sub = parser.add_subparsers()
        unwire.add_parser(sub)
        args = parser.parse_args(["unwire", "--yes", "--remove-index"])
        assert args.assume_yes is True
        assert args.remove_index is True
        args = parser.parse_args(["unwire"])
        assert args.assume_yes is False
        assert args.remove_index is False


class TestMaybeRemoveCodeIndex:
    def test_unregistered_repo_is_a_noop(self, ci_env: tuple[Path, Path, str]) -> None:
        repo, _, _ = ci_env
        assert unwire._maybe_remove_code_index(repo, assume_yes=False, remove_index=False) is None

    def test_non_tty_default_keeps_index(self, ci_env: tuple[Path, Path, str]) -> None:
        repo, data_root, slug = ci_env
        slug_dir = _register(data_root, slug, repo)
        result = unwire._maybe_remove_code_index(repo, assume_yes=False, remove_index=False)
        assert result == {"slug": slug, "removed": False, "kept": "default"}
        assert slug_dir.exists() and _registry_has(data_root, slug)

    def test_yes_flag_alone_keeps_index(self, ci_env: tuple[Path, Path, str]) -> None:
        repo, data_root, slug = ci_env
        slug_dir = _register(data_root, slug, repo)
        result = unwire._maybe_remove_code_index(repo, assume_yes=True, remove_index=False)
        assert result == {"slug": slug, "removed": False, "kept": "default"}
        assert slug_dir.exists() and _registry_has(data_root, slug)

    def test_tty_decline_default_keeps_index(
        self, ci_env: tuple[Path, Path, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo, data_root, slug = ci_env
        slug_dir = _register(data_root, slug, repo)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda prompt="": "")  # blank = default No
        result = unwire._maybe_remove_code_index(repo, assume_yes=False, remove_index=False)
        assert result == {"slug": slug, "removed": False, "kept": "declined"}
        assert slug_dir.exists() and _registry_has(data_root, slug)

    def test_tty_accept_removes_via_service(
        self, ci_env: tuple[Path, Path, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo, data_root, slug = ci_env
        _register(data_root, slug, repo)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda prompt="": "y")
        deleted: list[str] = []

        def _via_service(s: str, port: int) -> bool:
            deleted.append(s)
            return True

        monkeypatch.setattr(unwire, "_remove_index_via_service", _via_service)
        result = unwire._maybe_remove_code_index(repo, assume_yes=False, remove_index=False)
        assert result == {"slug": slug, "removed": True}
        assert deleted == [slug]

    def test_remove_index_flag_service_down_falls_back_to_direct(
        self, ci_env: tuple[Path, Path, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo, data_root, slug = ci_env
        slug_dir = _register(data_root, slug, repo)
        monkeypatch.setattr(unwire, "_remove_index_via_service", lambda s, p: None)  # unreachable
        result = unwire._maybe_remove_code_index(repo, assume_yes=True, remove_index=True)
        assert result == {"slug": slug, "removed": True}
        assert not slug_dir.exists()
        assert not _registry_has(data_root, slug)

    def test_direct_removal_refuses_while_job_active(
        self,
        ci_env: tuple[Path, Path, str],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        repo, data_root, slug = ci_env
        slug_dir = _register(data_root, slug, repo, active_job=True)
        monkeypatch.setattr(unwire, "_remove_index_via_service", lambda s, p: None)
        result = unwire._maybe_remove_code_index(repo, assume_yes=True, remove_index=True)
        assert result == {"slug": slug, "removed": False}
        assert slug_dir.exists() and _registry_has(data_root, slug)
        err = capsys.readouterr().err
        assert "ERROR:" in err and "active" in err
