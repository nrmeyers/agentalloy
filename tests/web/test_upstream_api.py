"""Unit tests for the per-repo upstream config endpoints (GET/PUT /api/upstream).

These cover the backend half of the "per-repo upstream via web UI" work item:
the ``/api/upstream`` surface reads and edits the *active chat* entry of a
repo's ``.agentalloy/upstream`` file, scoped by ``?repo_root=``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentalloy.api.proxy_context import read_upstream
from agentalloy.app import create_app

_CSRF = {"X-AgentAlloy-CSRF": "1"}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Point the user-scoped config/data dirs at tmp so create_app's default
    # paths never touch the real home (the endpoint itself is file-only, but
    # create_app is shared with the config tests' expectations).
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    app = create_app(use_default_lifespan=False)
    with TestClient(app) as c:
        yield c


def _write_upstream(root: Path, text: str) -> Path:
    (root / ".agentalloy").mkdir(parents=True, exist_ok=True)
    p = root / ".agentalloy" / "upstream"
    p.write_text(text)
    return p


def _repo(root: Path) -> str:
    return f"/api/upstream?repo_root={root}"


# --- GET ---------------------------------------------------------------


def test_get_valid_namespaced(client, tmp_path: Path):
    _write_upstream(
        tmp_path,
        "claude-code:\n  url: https://api.anthropic.com\n  model: c1\n"
        "qwen-code:\n  url: http://h:9000/v1\n  model: m1\n  key_env: OPENAI_API_KEY\n",
    )
    body = client.get(_repo(tmp_path)).json()
    assert body["exists"] is True
    assert body["harness"] == "qwen-code"
    assert body["url"] == "http://h:9000/v1"
    assert body["model"] == "m1"
    assert body["key_env"] == "OPENAI_API_KEY"
    assert body["detail"] is None


def test_get_legacy_flat_is_chat_scope(client, tmp_path: Path):
    _write_upstream(tmp_path, "url: http://h:9000/v1\nmodel: m1\n")
    body = client.get(_repo(tmp_path)).json()
    assert body["exists"] is True
    assert body["harness"] == ""  # flat file: the top level is the chat scope
    assert body["url"] == "http://h:9000/v1"
    assert body["model"] == "m1"
    assert body["key_env"] is None


def test_get_absent_file(client, tmp_path: Path):
    body = client.get(_repo(tmp_path)).json()
    assert body["exists"] is False
    assert body["harness"] is None
    assert body["url"] is None
    assert body["model"] is None


def test_get_passthrough_only_is_absent(client, tmp_path: Path):
    _write_upstream(
        tmp_path,
        "claude-code:\n  url: https://api.anthropic.com\n  model: c1\n"
        "codex:\n  url: https://api.openai.com\n  model: o1\n",
    )
    body = client.get(_repo(tmp_path)).json()
    assert body["exists"] is False


def test_get_malformed_returns_detail_not_500(client, tmp_path: Path):
    _write_upstream(tmp_path, "url: [unclosed\n")
    r = client.get(_repo(tmp_path))
    assert r.status_code == 200
    body = r.json()
    assert body["exists"] is True
    assert body["detail"]
    assert body["url"] is None


# --- PUT ---------------------------------------------------------------


def test_put_requires_csrf_header(client, tmp_path: Path):
    path = _write_upstream(tmp_path, "url: http://h:9000/v1\nmodel: m1\n")
    before = path.read_text()
    r = client.put(_repo(tmp_path), json={"url": "http://x", "model": "m2"})
    assert r.status_code == 403
    assert path.read_text() == before  # unchanged


def test_put_updates_namespaced_preserving_others(client, tmp_path: Path):
    _write_upstream(
        tmp_path,
        "claude-code:\n  url: https://api.anthropic.com\n  model: c1\n"
        "qwen-code:\n  url: http://h:9000/v1\n  model: m1\n  key_env: OPENAI_API_KEY\n",
    )
    r = client.put(
        _repo(tmp_path),
        json={"url": "http://new:1/v1", "model": "m2", "key_env": "OTHER_KEY"},
        headers=_CSRF,
    )
    assert r.status_code == 200
    body = client.get(_repo(tmp_path)).json()
    assert body["harness"] == "qwen-code"
    assert body["url"] == "http://new:1/v1"
    assert body["model"] == "m2"
    assert body["key_env"] == "OTHER_KEY"
    # The passthrough entry must be untouched.
    assert read_upstream(tmp_path, harness="claude-code").upstream.model == "c1"


def test_put_legacy_flat_updates_in_place(client, tmp_path: Path):
    _write_upstream(tmp_path, "url: http://h:9000/v1\nmodel: m1\n")
    r = client.put(_repo(tmp_path), json={"url": "http://new:1/v1", "model": "m2"}, headers=_CSRF)
    assert r.status_code == 200
    body = client.get(_repo(tmp_path)).json()
    assert body["harness"] == ""
    assert body["url"] == "http://new:1/v1"
    assert body["model"] == "m2"


def test_put_no_active_entry_is_400_and_creates_nothing(client, tmp_path: Path):
    # No file at all: nothing to edit (edit-only, no create).
    r = client.put(_repo(tmp_path), json={"url": "http://x", "model": "m"}, headers=_CSRF)
    assert r.status_code == 400
    assert not (tmp_path / ".agentalloy" / "upstream").exists()

    # Passthrough-only: still no chat entry to edit.
    _write_upstream(tmp_path, "claude-code:\n  url: https://api.anthropic.com\n  model: c1\n")
    r = client.put(_repo(tmp_path), json={"url": "http://x", "model": "m"}, headers=_CSRF)
    assert r.status_code == 400


def test_put_key_env_is_a_name_not_a_secret(client, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MY_UPSTREAM_KEY", "sk-super-secret")
    _write_upstream(tmp_path, "url: http://h:9000/v1\nmodel: m1\n")
    r = client.put(
        _repo(tmp_path),
        json={"url": "http://h:9000/v1", "model": "m1", "key_env": "MY_UPSTREAM_KEY"},
        headers=_CSRF,
    )
    assert r.status_code == 200
    # The file stores the variable NAME, never the secret value.
    text = (tmp_path / ".agentalloy" / "upstream").read_text()
    assert "MY_UPSTREAM_KEY" in text
    assert "sk-super-secret" not in text
    # And the endpoint never echoes the secret back.
    body = client.get(_repo(tmp_path)).json()
    assert body["key_env"] == "MY_UPSTREAM_KEY"
    assert "sk-super-secret" not in str(body)


def test_put_then_read_upstream_sees_new_values(client, tmp_path: Path):
    _write_upstream(tmp_path, "url: http://h:9000/v1\nmodel: m1\n")
    client.put(_repo(tmp_path), json={"url": "http://new:1/v1", "model": "m9"}, headers=_CSRF)
    # The proxy's per-request read path sees the edit with no restart.
    chat = read_upstream(tmp_path)
    assert chat.kind == "valid" and chat.upstream is not None
    assert chat.upstream.url == "http://new:1/v1"
    assert chat.upstream.model == "m9"


def test_put_writes_valid_yaml_no_tmp_left(client, tmp_path: Path):
    path = _write_upstream(tmp_path, "url: http://h:9000/v1\nmodel: m1\n")
    client.put(_repo(tmp_path), json={"url": "http://new:1/v1", "model": "m2"}, headers=_CSRF)
    # Atomic write: the file is valid YAML and no stale .tmp lingers.
    import yaml

    assert isinstance(yaml.safe_load(path.read_text()), dict)
    assert not list((tmp_path / ".agentalloy").glob("*.tmp"))


def test_put_rejects_empty_url_or_model(client, tmp_path: Path):
    path = _write_upstream(tmp_path, "url: http://h:9000/v1\nmodel: m1\n")
    before = path.read_text()
    r = client.put(_repo(tmp_path), json={"url": "", "model": "m2"}, headers=_CSRF)
    assert r.status_code == 400
    assert path.read_text() == before
    r = client.put(_repo(tmp_path), json={"url": "http://x", "model": ""}, headers=_CSRF)
    assert r.status_code == 400
    assert path.read_text() == before
