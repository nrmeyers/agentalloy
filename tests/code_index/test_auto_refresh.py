"""Code-index auto-refresh: staleness-driven incremental reindex (Gap 1)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from agentalloy.code_index.api import state as state_mod
from agentalloy.code_index.api.state import CodeIndexState
from agentalloy.code_index.staleness import Staleness
from agentalloy.config import Settings


@dataclass
class _Repo:
    slug: str
    repo_path: str
    head_sha: str | None


class _StubJobs:
    """Minimal jobs store: only what refresh_stale_repos touches."""

    def __init__(self, repos: list[_Repo], active: set[str] | None = None) -> None:
        self._repos = repos
        self._active = active or set()

    def list_repos(self) -> list[_Repo]:
        return self._repos

    def find_active(self, slug: str) -> object | None:
        return object() if slug in self._active else None


def _state(repos: list[_Repo], active: set[str] | None = None) -> CodeIndexState:
    return CodeIndexState(
        settings=Settings(),
        embed_client=object(),  # type: ignore[arg-type]
        jobs=_StubJobs(repos, active),  # type: ignore[arg-type]
    )


def _spy_start_job(state: CodeIndexState) -> list[tuple[Path, str, bool]]:
    calls: list[tuple[Path, str, bool]] = []

    def _fake(*, repo_path: Path, slug: str, force: bool, index_markdown: bool = True) -> Any:
        calls.append((repo_path, slug, force))
        return None

    state.start_job = _fake  # type: ignore[method-assign]
    return calls


def _pin_staleness(monkeypatch: pytest.MonkeyPatch, stale_shas: set[str | None]) -> None:
    def _fake(repo_path: Path, stored_sha: str | None) -> Staleness:
        return Staleness(stale=stored_sha in stale_shas, commits_behind=1)

    monkeypatch.setattr(state_mod, "check_staleness", _fake, raising=False)
    # refresh imports it lazily from the source module:
    monkeypatch.setattr("agentalloy.code_index.staleness.check_staleness", _fake)


def test_kicks_incremental_for_stale_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    repos = [_Repo("alpha", str(a), "sha-a"), _Repo("beta", str(b), "sha-b")]
    st = _state(repos)
    calls = _spy_start_job(st)
    _pin_staleness(monkeypatch, {"sha-a"})  # only alpha is stale

    kicked = st.refresh_stale_repos()

    assert kicked == ["alpha"]
    # force=False → INCREMENTAL, and the REGISTRY slug is reused (never re-derived).
    assert calls == [(a, "alpha", False)]


def test_skips_repo_with_active_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    a = tmp_path / "a"
    a.mkdir()
    st = _state([_Repo("alpha", str(a), "sha-a")], active={"alpha"})
    calls = _spy_start_job(st)
    _pin_staleness(monkeypatch, {"sha-a"})  # stale, but a job is already running

    assert st.refresh_stale_repos() == []
    assert calls == []


def test_skips_missing_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gone = tmp_path / "gone"  # never created
    st = _state([_Repo("ghost", str(gone), "sha")])
    calls = _spy_start_job(st)
    _pin_staleness(monkeypatch, {"sha"})

    assert st.refresh_stale_repos() == []
    assert calls == []


def test_fresh_repo_not_kicked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    a = tmp_path / "a"
    a.mkdir()
    st = _state([_Repo("alpha", str(a), "sha-a")])
    calls = _spy_start_job(st)
    _pin_staleness(monkeypatch, set())  # nothing stale

    assert st.refresh_stale_repos() == []
    assert calls == []


def test_registry_read_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Boom:
        def list_repos(self) -> list[_Repo]:
            raise RuntimeError("registry down")

    st = CodeIndexState(
        settings=Settings(),
        embed_client=object(),  # type: ignore[arg-type]
        jobs=_Boom(),  # type: ignore[arg-type]
    )
    assert st.refresh_stale_repos() == []  # no raise


def test_per_repo_error_does_not_stop_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    st = _state([_Repo("alpha", str(a), "sha-a"), _Repo("beta", str(b), "sha-b")])
    _pin_staleness(monkeypatch, {"sha-a", "sha-b"})

    calls: list[str] = []

    def _flaky(*, repo_path: Path, slug: str, force: bool, index_markdown: bool = True) -> Any:
        if slug == "alpha":
            raise RuntimeError("pipeline boom")
        calls.append(slug)
        return None

    st.start_job = _flaky  # type: ignore[method-assign]
    kicked = st.refresh_stale_repos()

    assert kicked == ["beta"]  # alpha's failure didn't abort the loop
    assert calls == ["beta"]


def test_config_default_off_env_override() -> None:
    assert Settings().code_index_refresh_seconds == 0
    import os
    from unittest.mock import patch

    with patch.dict(os.environ, {"CODE_INDEX_REFRESH_SECONDS": "300"}):
        assert Settings().code_index_refresh_seconds == 300
