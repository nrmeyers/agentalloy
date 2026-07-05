"""Module toggle tests — conditional router registration + /health modules block.

AgentAlloy serves independent context modules from one process. Each module's
routers register only when its toggle is on (COMPOSE_ENABLED, CODE_INDEX_ENABLED);
a disabled module's endpoints 404 and /health reports per-module status.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentalloy.app import create_app
from agentalloy.config import Settings


def _routes(client: TestClient) -> set[str]:
    return {getattr(r, "path", "") for r in client.app.routes}  # type: ignore[attr-defined]


def test_defaults_compose_on_code_index_off(client_env: None) -> None:
    app = create_app(use_default_lifespan=False)
    with TestClient(app) as client:
        paths = _routes(client)
        assert "/compose" in paths
        assert not any(p.startswith("/code/") for p in paths)
        body = client.get("/health").json()
        assert body["modules"] == {"compose": "enabled", "code_index": "disabled"}


def test_compose_disabled_unregisters_routers(
    client_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COMPOSE_ENABLED", "0")
    app = create_app(use_default_lifespan=False)
    with TestClient(app) as client:
        # Unregistered routes fall through to the SPA's catch-all static
        # mount, so assert on route registration rather than status codes.
        paths = _routes(client)
        assert "/compose" not in paths
        assert "/retrieve" not in paths
        # Health stays up regardless of module toggles.
        body = client.get("/health").json()
        assert body["status"] == "healthy"
        assert body["modules"]["compose"] == "disabled"


def test_code_index_enabled_mounts_module(
    client_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # PR4+: the module ships (routers + ingest pipeline). With the toggle on
    # it mounts under /code and reports "enabled". (The "unavailable" degrade
    # path still exists for installs without the [code-index] extra — it
    # can't be exercised here because this environment has the extra.)
    monkeypatch.setenv("CODE_INDEX_ENABLED", "1")
    app = create_app(use_default_lifespan=False)
    with TestClient(app) as client:
        body = client.get("/health").json()
        assert body["modules"]["code_index"] == "enabled"
        assert any(p.startswith("/code/") for p in _routes(client))


def test_code_index_data_dir_created_only_when_enabled(
    client_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_dir = tmp_path / "ci-data"
    monkeypatch.setenv("CODE_INDEX_DATA_DIR", str(data_dir))

    Settings().ensure_data_dirs()
    assert not data_dir.exists()

    monkeypatch.setenv("CODE_INDEX_ENABLED", "1")
    Settings().ensure_data_dirs()
    assert data_dir.is_dir()


def test_settings_defaults() -> None:
    s = Settings()
    assert s.compose_enabled is True
    assert s.code_index_enabled is False
    assert s.code_index_watch is False
    assert s.code_index_data_dir.endswith("agentalloy/code_index")


@pytest.fixture
def client_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Keep any ambient toggles out of the picture and steer default data
    # paths away from the real user dirs.
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    for var in ("COMPOSE_ENABLED", "CODE_INDEX_ENABLED", "CODE_INDEX_WATCH"):
        monkeypatch.delenv(var, raising=False)
