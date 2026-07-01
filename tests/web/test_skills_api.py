"""Unit tests for the web UI skill browser/editor endpoints + signal simulator."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

from agentalloy.app import create_app
from agentalloy.reads.models import ActiveSkill

_CSRF = {"X-AgentAlloy-CSRF": "1"}


def _skill(sid: str, klass: str = "domain", **kw) -> ActiveSkill:
    defaults = dict(
        canonical_name=sid.title(),
        category="engineering",
        skill_class=klass,
        domain_tags=["x"],
        always_apply=False,
        phase_scope=None,
        category_scope=None,
        active_version_id=f"{sid}-v1",
        tier=None,
        description=None,
    )
    defaults.update(kw)
    return ActiveSkill(skill_id=sid, **defaults)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("PROFILE_ROOT", str(tmp_path / "profiles"))
    app = create_app(use_default_lifespan=False)
    app.state.runtime = SimpleNamespace(
        get_active_skills=lambda: [
            _skill("pytest-idioms"),
            _skill("sys-vcs-forge", klass="system", category="tooling"),
            _skill("sdd-fast", klass="workflow", category="operational", phase_scope=None),
        ]
    )
    with TestClient(app) as c:
        yield c


def test_list_skills_filters_and_provenance(client):
    body = client.get("/api/skills").json()
    assert body["total"] == 3
    by_id = {s["skill_id"]: s for s in body["skills"]}
    # Shipped-pack provenance comes from the bundled pack manifests.
    assert by_id["sdd-fast"]["pack"] == "sdd"
    assert by_id["sys-vcs-forge"]["pack"] == "sys"
    # Filters
    assert client.get("/api/skills?class=system").json()["total"] == 1
    assert client.get("/api/skills?q=vcs").json()["total"] == 1
    assert client.get("/api/skills?category=engineering").json()["total"] == 1


def test_versions_endpoint_reads_store(client):
    rows = [
        ("v2", 2, "2026-06-01", "navistone", "second", "active", "prose two"),
        ("v1", 1, "2026-05-01", "navistone", "first", "superseded", "prose one"),
    ]

    def execute(sql: str, params=None):
        if "current_version_id" in sql:
            return [("v2",)]
        return rows

    client.app.state.store = SimpleNamespace(execute=execute)
    body = client.get("/api/skills/pytest-idioms/versions").json()
    assert [v["version_number"] for v in body["versions"]] == [2, 1]
    assert body["versions"][0]["is_active"] is True
    assert body["versions"][1]["is_active"] is False


def test_versions_404_unknown_skill(client):
    client.app.state.store = SimpleNamespace(execute=lambda sql, params=None: [])
    assert client.get("/api/skills/nope/versions").status_code == 404


def test_override_get_shows_layers_and_locked_fields(client):
    body = client.get("/api/skills/sdd-fast/override").json()
    assert body["active_layer"] == "default"
    assert body["shipped_raw_prose"] and "fast lane" in body["shipped_raw_prose"].lower()
    assert "exit_gates" in body["locked_fields"]
    assert any("phase set qa" in inv for inv in body["prose_invariants"])


def test_override_put_requires_csrf(client):
    r = client.put("/api/skills/sdd-fast/override", json={"raw_prose": "x" * 100})
    assert r.status_code == 403


def test_override_put_rejects_dropped_invariant(client):
    # Prose that drops the load-bearing `agentalloy phase set qa` command.
    r = client.put(
        "/api/skills/sdd-fast/override",
        json={"raw_prose": "Just vibe it out and ship whenever. " * 5},
        headers=_CSRF,
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["error"] == "validation_failed"
    assert any("phase set qa" in e for e in detail["errors"])


def test_override_put_then_delete_roundtrip(client, tmp_path: Path):
    shipped = client.get("/api/skills/sdd-fast/override").json()["shipped_raw_prose"]
    edited = shipped + "\n\n## Team addendum\n\nAlways run the smoke suite first.\n"

    r = client.put("/api/skills/sdd-fast/override", json={"raw_prose": edited}, headers=_CSRF)
    assert r.status_code == 200, r.json()
    path = Path(r.json()["path"])
    assert path.is_file()
    assert yaml.safe_load(path.read_text())["raw_prose"].endswith("smoke suite first.\n")
    assert client.get("/api/skills/sdd-fast/override").json()["active_layer"] == "profile"

    r = client.delete("/api/skills/sdd-fast/override?layer=profile", headers=_CSRF)
    assert r.status_code == 200
    assert not path.exists()
    assert client.get("/api/skills/sdd-fast/override").json()["active_layer"] == "default"


def test_signal_evaluate_read_only(client, tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / ".agentalloy").mkdir(parents=True)
    (repo / ".agentalloy" / "phase").write_text("phase: build\nschema_version: 1\n")

    r = client.post(
        "/api/signal/evaluate", json={"repo": str(repo), "prompt": "how do I write a test?"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["phase"] == "build"
    assert isinstance(body["should_compose"], bool)
    # Read-only: the simulator must not have advanced the phase or bumped
    # banner counters.
    assert "build" in (repo / ".agentalloy" / "phase").read_text()
    assert not (repo / ".agentalloy" / "banner_turn").exists()


def test_signal_evaluate_rejects_missing_repo(client, tmp_path: Path):
    r = client.post("/api/signal/evaluate", json={"repo": str(tmp_path / "nope"), "prompt": "hi"})
    assert r.status_code == 400
