"""Tests for the SPA dist-dir resolution and mounting (web/spa.py)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentalloy import __version__
from agentalloy.web import spa


def _dist(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "index.html").write_text("<html>ui</html>")
    return path


@pytest.fixture(autouse=True)
def _no_repo_dist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the repo-layout probe at an empty tmp tree so a developer's local
    ``frontend/dist`` build can't leak into these tests."""
    fake_module = tmp_path / "repo" / "src" / "agentalloy" / "web" / "spa.py"
    monkeypatch.setattr(spa, "__file__", str(fake_module))


def test_env_override_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    override = _dist(tmp_path / "override")
    monkeypatch.setenv("AGENTALLOY_WEB_DIST", str(override))
    assert spa._dist_dir() == override


def test_env_override_without_index_disables(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("AGENTALLOY_WEB_DIST", str(empty))
    # An explicit-but-broken override must not fall through to other locations.
    _dist(tmp_path / "data" / "agentalloy" / "web-dist" / __version__)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert spa._dist_dir() is None


def test_repo_layout_resolves(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AGENTALLOY_WEB_DIST", raising=False)
    repo_dist = _dist(tmp_path / "repo" / "frontend" / "dist")
    assert spa._dist_dir() == repo_dist


def test_pulled_bundle_resolves_version_matched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("AGENTALLOY_WEB_DIST", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    _dist(tmp_path / "data" / "agentalloy" / "web-dist" / "0.0.0-other")
    assert spa._dist_dir() is None  # only the installed version's bundle serves
    pulled = _dist(tmp_path / "data" / "agentalloy" / "web-dist" / __version__)
    assert spa._dist_dir() == pulled


def test_mount_serves_spa(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGENTALLOY_WEB_DIST", str(_dist(tmp_path / "dist")))
    app = FastAPI()
    spa.mount_web_ui(app)
    resp = TestClient(app).get("/")
    assert resp.status_code == 200
    assert "ui" in resp.text


def test_mount_501_hint_when_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AGENTALLOY_WEB_DIST", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    app = FastAPI()
    spa.mount_web_ui(app)
    resp = TestClient(app).get("/")
    assert resp.status_code == 501
    assert "pull-web" in resp.json()["detail"]
