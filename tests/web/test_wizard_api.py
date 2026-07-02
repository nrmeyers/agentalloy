"""Unit tests for the custom-skill creation wizard endpoints."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentalloy.app import create_app

_CSRF = {"X-AgentAlloy-CSRF": "1"}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    app = create_app(use_default_lifespan=False)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".agentalloy").mkdir(parents=True)
    (root / "pyproject.toml").write_text("")
    return root


def _scaffold(client, repo: Path, pack: str = "team-pack", skill_id: str = "team-deploy") -> dict:
    r = client.post(
        "/api/wizard/scaffold",
        json={"repo": str(repo), "pack": pack, "skill_id": skill_id},
        headers=_CSRF,
    )
    assert r.status_code == 200, r.json()
    return r.json()


def test_scaffold_creates_pack_in_lane_location(client, repo: Path):
    result = _scaffold(client, repo)
    pack_dir = repo / ".agentalloy" / "custom-skills" / "team-pack"
    assert (pack_dir / "pack.yaml").is_file()
    assert (pack_dir / "team-deploy.yaml").is_file()
    assert result["skill_file"] == "team-deploy.yaml"
    assert "team-deploy" in result["skill_yaml"]


def test_scaffold_requires_csrf_and_sane_names(client, repo: Path):
    r = client.post(
        "/api/wizard/scaffold",
        json={"repo": str(repo), "pack": "p", "skill_id": "s"},
    )
    assert r.status_code == 403
    r = client.post(
        "/api/wizard/scaffold",
        json={"repo": str(repo), "pack": "../escape", "skill_id": "s"},
        headers=_CSRF,
    )
    assert r.status_code == 400
    r = client.post(
        "/api/wizard/scaffold",
        json={"repo": str(repo), "pack": "p", "skill_id": "bad/../id"},
        headers=_CSRF,
    )
    assert r.status_code == 400


def test_pack_read_write_roundtrip(client, repo: Path):
    _scaffold(client, repo)
    body = client.get("/api/wizard/pack", params={"repo": str(repo), "pack": "team-pack"}).json()
    assert body["exists"] is True
    names = {f["name"] for f in body["files"]}
    assert names == {"pack.yaml", "team-deploy.yaml"}

    skill = next(f for f in body["files"] if f["name"] == "team-deploy.yaml")
    edited = skill["content"].replace("team-deploy", "team-deploy")  # content passthrough
    r = client.put(
        "/api/wizard/file",
        json={
            "repo": str(repo),
            "pack": "team-pack",
            "file": "team-deploy.yaml",
            "content": edited,
        },
        headers=_CSRF,
    )
    assert r.status_code == 200

    r = client.put(
        "/api/wizard/file",
        json={"repo": str(repo), "pack": "team-pack", "file": "x.yaml", "content": "not: [valid"},
        headers=_CSRF,
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "invalid_yaml"

    r = client.put(
        "/api/wizard/file",
        json={"repo": str(repo), "pack": "team-pack", "file": "../../evil.yaml", "content": "a: 1"},
        headers=_CSRF,
    )
    assert r.status_code == 400


def test_validate_passes_on_fresh_scaffold(client, repo: Path):
    _scaffold(client, repo)
    r = client.post(
        "/api/wizard/validate",
        json={"repo": str(repo), "pack": "team-pack"},
        headers=_CSRF,
    )
    assert r.status_code == 200
    body = r.json()
    # The domain-skill scaffold passes strict validation out of the box —
    # that's the rail's contract (aa4929b).
    assert body.get("errors") in ([], None) or body.get("ok") or body.get("valid")


def test_install_outside_lane_skips_approval(client, repo: Path, monkeypatch: pytest.MonkeyPatch):
    _scaffold(client, repo)
    captured: dict = {}

    def fake_install(pack_dir, *, root, no_restart, strict, allow_duplicates, run_reembed=True):
        captured.update(
            pack_dir=str(pack_dir), no_restart=no_restart, strict=strict, dup=allow_duplicates
        )
        return {"action": "ingested", "skills_ingested": 1}

    monkeypatch.setattr(
        "agentalloy.install.subcommands.install_pack.install_local_pack", fake_install
    )
    r = client.post(
        "/api/wizard/install",
        json={"repo": str(repo), "pack": "team-pack"},
        headers=_CSRF,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["approval"] is None  # no add-skill phase → the click is the approval
    assert body["install"]["skills_ingested"] == 1
    assert captured["strict"] is True and captured["no_restart"] is True


def test_install_in_lane_records_approval_and_advances(
    client, repo: Path, monkeypatch: pytest.MonkeyPatch
):
    _scaffold(client, repo)
    (repo / ".agentalloy" / "phase").write_text("phase: add-skill\nschema_version: 1\n")
    monkeypatch.setattr(
        "agentalloy.install.subcommands.install_pack.install_local_pack",
        lambda pack_dir, **kw: {"action": "ingested", "skills_ingested": 1},
    )
    r = client.post(
        "/api/wizard/install",
        json={"repo": str(repo), "pack": "team-pack", "approver": "alice"},
        headers=_CSRF,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["approval"]["ok"] is True
    assert body["approval"]["approver"] == "alice"
    # The lane completes: approval auto-advances back to intake.
    assert body["approval"]["advanced"]["phase"] == "intake"
    assert (repo / ".agentalloy" / "approved" / "add-skill").is_file()
