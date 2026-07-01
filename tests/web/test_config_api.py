"""Unit tests for the web UI config endpoints (GET/PUT /api/config, reload)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentalloy.app import create_app

_CSRF = {"X-AgentAlloy-CSRF": "1"}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Point the user-scoped config/data dirs at tmp so env_path() and the
    # Settings default paths never touch the real home.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    # Reload mutates the real process env; pin the vars these tests touch so
    # monkeypatch teardown restores them for the rest of the worker.
    for var, val in (
        ("LOG_LEVEL", "INFO"),
        ("BOUNCE_BUDGET", "3"),
        ("SDD_FAST_REQUIRE_APPROVAL", "0"),
    ):
        monkeypatch.setenv(var, val)
    app = create_app(use_default_lifespan=False)
    with TestClient(app) as c:
        yield c


def _env_file(tmp_path: Path) -> Path:
    return tmp_path / "config" / "agentalloy" / ".env"


def test_get_config_masks_secret(client, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("UPSTREAM_API_KEY", "sk-secret")
    body = client.get("/api/config").json()
    assert body["upstream_api_key"] == "***"
    assert "sk-secret" not in str(body)
    assert body["env_file_path"].endswith("/.env")
    # Read-only paths are present for display.
    assert body["telemetry_db_path"].endswith("telemetry.duck")


def test_get_config_null_secret_when_unset(client, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("UPSTREAM_API_KEY", raising=False)
    assert client.get("/api/config").json()["upstream_api_key"] is None


def test_put_requires_csrf_header(client):
    r = client.put("/api/config", json={"log_level": "DEBUG"})
    assert r.status_code == 403


def test_put_rejects_unknown_and_readonly_fields(client):
    r = client.put("/api/config", json={"duckdb_path": "/x"}, headers=_CSRF)
    assert r.status_code == 400
    assert "read-only" in r.json()["detail"]["detail"]


def test_put_validates_log_level(client):
    r = client.put("/api/config", json={"log_level": "LOUD"}, headers=_CSRF)
    assert r.status_code == 400
    assert "log_level" in r.json()["detail"]["detail"]


def test_put_validates_threshold_cross_field(client):
    r = client.put(
        "/api/config",
        json={"dedup_hard_threshold": 0.5, "dedup_soft_threshold": 0.9},
        headers=_CSRF,
    )
    assert r.status_code == 400
    assert "dedup_hard_threshold" in r.json()["detail"]["detail"]


def test_put_writes_env_and_reload_applies(client, tmp_path: Path):
    r = client.put(
        "/api/config",
        json={"log_level": "DEBUG", "bounce_budget": 5, "sdd_fast_require_approval": True},
        headers=_CSRF,
    )
    assert r.status_code == 200
    env_file = _env_file(tmp_path)
    content = env_file.read_text()
    assert "LOG_LEVEL=DEBUG" in content
    assert "BOUNCE_BUDGET=5" in content
    assert "SDD_FAST_REQUIRE_APPROVAL=1" in content

    r = client.post("/api/config/reload", headers=_CSRF)
    assert r.status_code == 200
    assert os.environ["LOG_LEVEL"] == "DEBUG"
    assert client.get("/api/config").json()["bounce_budget"] == 5


def test_put_preserves_unknown_env_lines(client, tmp_path: Path):
    env_file = _env_file(tmp_path)
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("# hand comment\nCUSTOM_KEY=keepme\nLOG_LEVEL=INFO\n")

    client.put("/api/config", json={"log_level": "ERROR"}, headers=_CSRF)

    content = env_file.read_text()
    assert "# hand comment" in content
    assert "CUSTOM_KEY=keepme" in content
    assert "LOG_LEVEL=ERROR" in content
    assert "LOG_LEVEL=INFO" not in content


def test_put_masked_secret_is_noop_for_that_field(client, tmp_path: Path):
    r = client.put(
        "/api/config", json={"upstream_api_key": "***", "log_level": "DEBUG"}, headers=_CSRF
    )
    assert r.status_code == 200
    assert "UPSTREAM_API_KEY" not in _env_file(tmp_path).read_text()


def test_put_null_clears_nullable_field(client, tmp_path: Path):
    env_file = _env_file(tmp_path)
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("FORCED_PROFILE=work\n")

    r = client.put("/api/config", json={"forced_profile": None}, headers=_CSRF)
    assert r.status_code == 200
    assert "FORCED_PROFILE" not in env_file.read_text()


def test_reload_without_env_file_is_500(client):
    r = client.post("/api/config/reload", headers=_CSRF)
    assert r.status_code == 500
    assert r.json()["detail"]["error"] == "reload_failed"
