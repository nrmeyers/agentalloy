"""T1 — POST /corpus/ingest-pack.

Exercises the endpoint's own logic — auth (AC-7), the off-switch, path-traversal
refusal, the pre-install dedup gate (AC-4/AC-5), the store.released() wrap, and
verbatim result passthrough (AC-10) — with the heavy seams (install_local_pack,
probe, embed, cache refresh) patched. The live-corpus write is the deferred
integration test (AC-2).
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from agentalloy.api import corpus_ingest_router as mod

SECRET = "test-ingest-secret"
GOOD_HEADERS = {"X-AgentAlloy-Ingest-Token": SECRET}
VALID_PACK = {
    "pack": {
        "pack.yaml": "name: p\nversion: 1.0.0\nskills:\n  - {skill_id: s1, file: s1.yaml}\n",
        "s1.yaml": "skill_id: s1\n",
    }
}


class _FakeStore:
    """Records whether the release window was entered around the install."""

    def __init__(self) -> None:
        self.released_count = 0

    @contextmanager
    def released(self):
        self.released_count += 1
        yield


@pytest.fixture
def released_flag():
    return {"count": 0}


@pytest.fixture
def client(monkeypatch, released_flag):
    """Bare app with the router mounted and every heavy seam stubbed."""
    monkeypatch.setattr(mod, "resolve_ingest_secret", lambda *, mint=False: SECRET)
    monkeypatch.setattr(mod, "_fragment_texts", lambda pack_dir: ["frag one", "frag two"])
    monkeypatch.setattr(mod, "_default_embed", lambda: lambda text: [0.0])
    monkeypatch.setattr(mod, "probe_lesson_duplicates", lambda *a, **k: [])  # no dups by default
    monkeypatch.setattr(mod, "refresh_runtime_cache", lambda app: True)

    store = _FakeStore()

    def _fake_install(pack_dir, **kwargs):
        released_flag["count"] = store.released_count  # captured mid-window
        released_flag["kwargs"] = kwargs
        return {"action": "already_installed", "pack": "p", "skills_ingested": 1}

    monkeypatch.setattr(mod, "install_local_pack", _fake_install)
    monkeypatch.delenv(mod.CORPUS_INGEST_ENV, raising=False)

    app = FastAPI()
    app.include_router(mod.router)
    app.state.store = store
    app.state.vector_store = object()
    return TestClient(app)


def test_happy_path_installs_and_passes_result_through(client, released_flag):
    r = client.post("/corpus/ingest-pack", json=VALID_PACK, headers=GOOD_HEADERS)
    assert r.status_code == 200
    assert r.json()["action"] == "already_installed"  # verbatim install result (AC-10)
    assert released_flag["count"] == 1, "install must run inside store.released()"
    kw = released_flag["kwargs"]
    assert kw["no_restart"] is True and kw["run_reembed"] is True


def test_missing_token_401_no_install(client, monkeypatch):
    calls: list[Any] = []
    monkeypatch.setattr(mod, "install_local_pack", lambda *a, **k: calls.append(1) or {})
    r = client.post("/corpus/ingest-pack", json=VALID_PACK)
    assert r.status_code == 401
    assert not calls


def test_wrong_token_401(client):
    r = client.post(
        "/corpus/ingest-pack", json=VALID_PACK, headers={"X-AgentAlloy-Ingest-Token": "nope"}
    )
    assert r.status_code == 401


def test_disabled_returns_404(client, monkeypatch):
    monkeypatch.setenv(mod.CORPUS_INGEST_ENV, "0")
    r = client.post("/corpus/ingest-pack", json=VALID_PACK, headers=GOOD_HEADERS)
    assert r.status_code == 404


def test_path_traversal_refused(client):
    bad = {"pack": {"../escape.yaml": "x"}}
    r = client.post("/corpus/ingest-pack", json=bad, headers=GOOD_HEADERS)
    assert r.status_code == 400


def test_reembed_flag_forwarded(client, released_flag):
    body = dict(VALID_PACK, reembed=False)
    r = client.post("/corpus/ingest-pack", json=body, headers=GOOD_HEADERS)
    assert r.status_code == 200
    assert released_flag["kwargs"]["run_reembed"] is False


def test_strict_flag_forwarded(client, released_flag):
    # install-pack --allow-lint-warnings -> strict=false must reach install_local_pack,
    # not be silently forced True by the endpoint.
    r = client.post("/corpus/ingest-pack", json=VALID_PACK, headers=GOOD_HEADERS)
    assert released_flag["kwargs"]["strict"] is True  # default
    body = dict(VALID_PACK, strict=False)
    r = client.post("/corpus/ingest-pack", json=body, headers=GOOD_HEADERS)
    assert r.status_code == 200
    assert released_flag["kwargs"]["strict"] is False


def test_hard_duplicate_refused_before_install(client, monkeypatch):
    class _Hit:
        skill_id = "existing-skill"

    installed: list[Any] = []
    monkeypatch.setattr(mod, "probe_lesson_duplicates", lambda *a, **k: [_Hit()])
    monkeypatch.setattr(mod, "install_local_pack", lambda *a, **k: installed.append(1) or {})
    r = client.post("/corpus/ingest-pack", json=VALID_PACK, headers=GOOD_HEADERS)
    assert r.status_code == 200
    assert r.json()["action"] == "duplicate_refused"
    assert r.json()["duplicates"] == ["existing-skill"]
    assert not installed, "must not install when a hard duplicate is found (AC-5)"


def test_allow_duplicates_skips_probe_and_installs(client, monkeypatch):
    probed: list[Any] = []
    monkeypatch.setattr(mod, "probe_lesson_duplicates", lambda *a, **k: probed.append(1) or [])
    body = dict(VALID_PACK, allow_duplicates=True)
    r = client.post("/corpus/ingest-pack", json=body, headers=GOOD_HEADERS)
    assert r.status_code == 200
    assert r.json()["action"] == "already_installed"
    assert not probed, "allow_duplicates bypasses the probe"


def test_probe_failure_fails_closed(client, monkeypatch):
    installed: list[Any] = []

    def _boom(*a, **k):
        raise RuntimeError("embed server down")

    monkeypatch.setattr(mod, "probe_lesson_duplicates", _boom)
    monkeypatch.setattr(mod, "install_local_pack", lambda *a, **k: installed.append(1) or {})
    r = client.post("/corpus/ingest-pack", json=VALID_PACK, headers=GOOD_HEADERS)
    assert r.status_code == 200
    assert r.json()["action"] == "dedup_probe_failed"
    assert not installed, "a failed probe must not install (fail closed)"
