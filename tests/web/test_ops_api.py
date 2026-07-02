"""Unit tests for the web UI ops endpoints — repos, approvals, packs, profiles."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agentalloy.app import create_app

_CSRF = {"X-AgentAlloy-CSRF": "1"}


def _make_repo(tmp_path: Path, name: str, phase: str = "spec") -> Path:
    root = tmp_path / name
    (root / ".agentalloy").mkdir(parents=True)
    (root / ".agentalloy" / "phase").write_text(f"phase: {phase}\nschema_version: 1\n")
    (root / "pyproject.toml").write_text("")
    return root


def _write_spec_doc(root: Path) -> Path:
    spec = root / "docs" / "spec"
    spec.mkdir(parents=True, exist_ok=True)
    doc = spec / "x.md"
    doc.write_text("# x\n## Acceptance Criteria\n- a\n## Out of Scope\n- b\n")
    return doc


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("PROFILE_ROOT", str(tmp_path / "profiles"))
    app = create_app(use_default_lifespan=False)
    with TestClient(app) as c:
        c.tmp = tmp_path  # pyright: ignore[reportAttributeAccessIssue]
        yield c


def _wire(monkeypatch: pytest.MonkeyPatch, *roots: Path) -> None:
    state = {
        "harness_files_written": [
            {"repo_root": str(r), "harness": "claude-code", "path": "x", "action": "written"}
            for r in roots
        ]
    }
    monkeypatch.setattr("agentalloy.install.state.load_state", lambda root=None: state)


def test_repos_lists_wired_state(client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = _make_repo(tmp_path, "r1", phase="build")
    (repo / ".agentalloy" / "config").write_text("lifecycle_mode: full\n")
    (repo / ".agentalloy" / "upstream").write_text("url: http://localhost:1234/v1\nmodel: qwen3\n")
    (repo / ".agentalloy" / "contracts" / "build").mkdir(parents=True)
    (repo / ".agentalloy" / "contracts" / "build" / "t.md").write_text("x")
    _wire(monkeypatch, repo)

    body = client.get("/api/repos").json()
    assert body["total"] == 1
    r = body["repos"][0]
    assert r["harnesses"] == ["claude-code"]
    assert r["phase"] == "build"
    assert r["lifecycle_mode"] == "full"
    assert r["upstream_model"] == "qwen3"
    assert r["contracts_by_phase"] == {"build": 1}
    assert r["approval_required"] is False  # build is not approval-gated


def test_repos_tolerates_missing_dir(client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _wire(monkeypatch, tmp_path / "gone")
    body = client.get("/api/repos").json()
    assert body["repos"][0]["exists"] is False


def test_approvals_pending_then_approve_advances(
    client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = _make_repo(tmp_path, "r2", phase="spec")
    _write_spec_doc(repo)
    _wire(monkeypatch, repo)

    pending = client.get("/api/approvals").json()
    assert pending["total"] == 1
    entry = pending["pending"][0]
    assert entry["phase"] == "spec"
    assert entry["next_phase"] == "design"
    assert entry["stale"] is False  # never approved, not stale
    assert entry["artifacts"] == ["docs/spec/x.md"]

    r = client.post("/api/repos/approve", json={"repo": str(repo), "phase": "spec"})
    assert r.status_code == 403  # CSRF required

    r = client.post(
        "/api/repos/approve",
        json={"repo": str(repo), "phase": "spec", "approver": "alice"},
        headers=_CSRF,
    )
    assert r.status_code == 200
    assert r.json()["advanced"]["phase"] == "design"
    assert client.get("/api/approvals").json()["total"] == 0


def test_approvals_stale_marker_reappears(client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = _make_repo(tmp_path, "r3", phase="spec")
    doc = _write_spec_doc(repo)
    _wire(monkeypatch, repo)
    r = client.post("/api/repos/approve", json={"repo": str(repo), "phase": "spec"}, headers=_CSRF)
    assert r.status_code == 200
    # Back to spec (simulate rework), edit the artifact after the marker.
    (repo / ".agentalloy" / "phase").write_text("phase: spec\nschema_version: 1\n")
    future = time.time() + 5
    import os

    os.utime(doc, (future, future))

    pending = client.get("/api/approvals").json()
    assert pending["total"] == 1
    assert pending["pending"][0]["stale"] is True


def test_gates_endpoint_reports_blockers(client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = _make_repo(tmp_path, "r4", phase="spec")  # no spec doc yet
    _wire(monkeypatch, repo)
    body = client.get(f"/api/repos/gates?repo={repo}").json()
    assert body["phase"] == "spec"
    assert body["next_phase"] == "design"
    assert body["blocked"] is True
    assert body["approval_pending"] is True


def test_packs_installed_counts(client):
    client.app.state.runtime = SimpleNamespace(
        get_active_skills=lambda: [SimpleNamespace(skill_id="sdd-fast")]
    )
    body = client.get("/api/packs").json()
    sdd = next(p for p in body["packs"] if p["name"] == "sdd")
    assert sdd["skill_count"] >= 8  # includes sdd-add-skill now
    assert sdd["installed_count"] == 1


def test_doctor_passthrough(client, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "agentalloy.install.subcommands.doctor.run_doctor",
        lambda: {"schema_version": 2, "all_checks_passed": True, "checks": []},
    )
    assert client.get("/api/doctor").json()["all_checks_passed"] is True


def test_reembed_status_and_dry_run(client, monkeypatch: pytest.MonkeyPatch):
    client.app.state.store = SimpleNamespace()
    client.app.state.vector_store = SimpleNamespace(count_embeddings=lambda: 42)
    monkeypatch.setattr(
        "agentalloy.reembed.cli.discover_unembedded_fragments",
        lambda store, vs, **kw: [1, 2, 3],
    )
    body = client.get("/api/reembed/status").json()
    assert body == {"embedded_total": 42, "unembedded": 3}
    r = client.post("/api/reembed", json={"dry_run": True}, headers=_CSRF)
    assert r.json() == {"dry_run": True, "would_embed": 3}


def test_reembed_run_releases_store_handle_around_write(client, monkeypatch: pytest.MonkeyPatch):
    """The service process holds the skill store read-only; the write pass must
    run inside released() (handle closed) and refresh the cache afterwards."""
    from contextlib import contextmanager

    events: list[str] = []

    class FakeStore:
        @contextmanager
        def released(self):
            events.append("released-enter")
            yield
            events.append("released-exit")

    client.app.state.store = FakeStore()
    monkeypatch.setattr(
        "agentalloy.reembed.cli.run_bulk_reembed",
        lambda **kw: events.append("reembed") or 0,
    )
    monkeypatch.setattr(
        "agentalloy.web.runtime_refresh.refresh_runtime_cache",
        lambda app: events.append("refresh") or True,
    )
    r = client.post("/api/reembed", json={"dry_run": False}, headers=_CSRF)
    assert r.status_code == 200
    body = r.json()
    assert body["exit_code"] == 0
    assert body["cache_refreshed"] is True
    assert events == ["released-enter", "reembed", "released-exit", "refresh"]


def test_profiles_list_and_resolve(client, tmp_path: Path):
    body = client.get("/api/profiles").json()
    assert any(p["name"] == "default" for p in body["profiles"])
    repo = _make_repo(tmp_path, "r5")
    r = client.post("/api/profiles/resolve", json={"repo": str(repo)})
    assert r.json()["profile"] == "default"
