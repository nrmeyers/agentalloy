"""Module wiring — CODE_INDEX_ENABLED=1 mounts /code and flips module_status."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentalloy.app import create_app
from agentalloy.code_index.api import build_code_index_router


@pytest.fixture
def enabled_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("CODE_INDEX_ENABLED", "1")
    monkeypatch.setenv("CODE_INDEX_DATA_DIR", str(tmp_path / "ci-data"))
    monkeypatch.delenv("CODE_INDEX_WATCH", raising=False)


def test_build_router_mounts_expected_routes() -> None:
    router = build_code_index_router()
    paths = {getattr(r, "path", "") for r in router.routes}
    assert paths == {
        "/code/index",
        "/code/index/jobs",
        "/code/index/{job_id}/status",
        "/code/index/{job_id}/cancel",
        "/code/index/{repo_slug}",
        "/code/repos",
        "/code/repos/{slug}/stats",
        "/code/repos/{slug}/reindex",
    }


def test_enabled_module_mounts_code_routes(enabled_env: None) -> None:
    app = create_app(use_default_lifespan=False)
    with TestClient(app) as client:
        paths = {getattr(r, "path", "") for r in app.routes}
        assert "/code/index" in paths
        assert "/code/repos" in paths
        body = client.get("/health").json()
        assert body["modules"]["code_index"] == "enabled"


def test_unbound_state_dependency_is_a_hard_error(enabled_env: None) -> None:
    # Without the lifespan (or a test override) the DI provider must fail
    # loudly rather than silently constructing stores.
    app = create_app(use_default_lifespan=False)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/code/repos")
        assert resp.status_code == 500
