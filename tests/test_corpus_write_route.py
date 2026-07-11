"""T2 — corpus-write routing + the ingest push client."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from agentalloy.install import corpus_write_route as mod
from agentalloy.install.corpus_write_route import (
    CorpusWriteRoute,
    decide_corpus_write_route,
    push_pack_to_service,
)


@pytest.fixture
def pack_dir(tmp_path: Path) -> Path:
    d = tmp_path / "pack"
    d.mkdir()
    (d / "pack.yaml").write_text("name: p\nversion: 1.0.0\n", encoding="utf-8")
    (d / "s1.yaml").write_text("skill_id: s1\n", encoding="utf-8")
    return d


# --- decide_corpus_write_route -------------------------------------------------


def test_route_via_service_when_reachable(monkeypatch):
    monkeypatch.setattr(mod, "DEFAULT_HOST", "127.0.0.1")
    route = decide_corpus_write_route(
        reachable_fn=lambda port: True,
        blocker_fn=lambda: "should not be consulted",
    )
    assert route.mode == "via_service"
    assert route.port  # resolved from deployment


def test_route_write_host_when_unreachable_and_writable(monkeypatch):
    class _Target:
        deployment = "native"
        port = 47950

    monkeypatch.setattr(
        "agentalloy.install.server_proc.resolve_deployment", lambda *a, **k: _Target()
    )
    route = decide_corpus_write_route(reachable_fn=lambda port: False, blocker_fn=lambda: None)
    assert route.mode == "write_host"


def test_route_blocked_preserves_reason(monkeypatch):
    class _Target:
        deployment = "native"
        port = 47950

    monkeypatch.setattr(
        "agentalloy.install.server_proc.resolve_deployment", lambda *a, **k: _Target()
    )
    msg = "the corpus is locked by the running AgentAlloy service."
    route = decide_corpus_write_route(reachable_fn=lambda port: False, blocker_fn=lambda: msg)
    assert route.mode == "blocked"
    assert route.reason == msg


def test_route_stopped_container_blocks_not_write_host(monkeypatch):
    """A container deployment that isn't serving must NOT fall to write_host: a
    host write lands in a different file than the in-volume corpus the container
    reads. It blocks with a start-the-container reason."""

    class _Target:
        deployment = "container"
        port = 47950

    monkeypatch.setattr(
        "agentalloy.install.server_proc.resolve_deployment", lambda *a, **k: _Target()
    )
    # blocker_fn would say "host writable" (None) — the container guard must win first.
    route = decide_corpus_write_route(reachable_fn=lambda port: False, blocker_fn=lambda: None)
    assert route.mode == "blocked"
    assert "container isn't running" in route.reason


def test_route_native_stopped_writes_host(monkeypatch):
    """Native deployment, service down, corpus writable → write_host (AC-8)."""

    class _Target:
        deployment = "native"
        port = 47950

    monkeypatch.setattr(
        "agentalloy.install.server_proc.resolve_deployment", lambda *a, **k: _Target()
    )
    route = decide_corpus_write_route(reachable_fn=lambda port: False, blocker_fn=lambda: None)
    assert route.mode == "write_host"


# --- push_pack_to_service ------------------------------------------------------


def _fake_secret(monkeypatch):
    monkeypatch.setattr(
        "agentalloy.install.ingest_secret.resolve_ingest_secret", lambda *, mint=False: "SEKRET"
    )


def test_push_success_returns_result(monkeypatch, pack_dir):
    _fake_secret(monkeypatch)
    captured = {}

    def fake_post(url, *, json, headers, timeout):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return httpx.Response(200, json={"action": "promoted", "skills_ingested": 1})

    res = push_pack_to_service(
        pack_dir, route=CorpusWriteRoute("via_service", port=47950), post_fn=fake_post
    )
    assert res["action"] == "promoted"
    assert captured["headers"]["X-AgentAlloy-Ingest-Token"] == "SEKRET"
    # Pack files are shipped as {relpath: content} bytes.
    assert set(captured["json"]["pack"]) == {"pack.yaml", "s1.yaml"}
    assert "47950/corpus/ingest-pack" in captured["url"]


def test_push_transport_error_maps_to_install_failed(monkeypatch, pack_dir):
    _fake_secret(monkeypatch)

    def boom(*a, **k):
        raise httpx.ConnectError("connection refused")

    res = push_pack_to_service(
        pack_dir, route=CorpusWriteRoute("via_service", port=47950), post_fn=boom
    )
    assert res["action"] == "install_failed"
    assert "could not reach" in res["error"]


def test_push_non_200_maps_to_install_failed(monkeypatch, pack_dir):
    _fake_secret(monkeypatch)

    def fake_post(*a, **k):
        return httpx.Response(401, text="invalid or missing ingest token")

    res = push_pack_to_service(
        pack_dir, route=CorpusWriteRoute("via_service", port=47950), post_fn=fake_post
    )
    assert res["action"] == "install_failed"
    assert "401" in res["error"]


def test_push_forwards_reembed_and_allow_duplicates(monkeypatch, pack_dir):
    _fake_secret(monkeypatch)
    captured = {}

    def fake_post(url, *, json, headers, timeout):
        captured.update(json)
        return httpx.Response(200, json={"action": "already_installed"})

    push_pack_to_service(
        pack_dir,
        route=CorpusWriteRoute("via_service", port=47950),
        allow_duplicates=True,
        reembed=False,
        post_fn=fake_post,
    )
    assert captured["allow_duplicates"] is True
    assert captured["reembed"] is False


def test_push_forwards_strict(monkeypatch, pack_dir):
    _fake_secret(monkeypatch)
    captured = {}

    def fake_post(url, *, json, headers, timeout):
        captured.update(json)
        return httpx.Response(200, json={"action": "ingested"})

    push_pack_to_service(
        pack_dir,
        route=CorpusWriteRoute("via_service", port=47950),
        strict=False,
        post_fn=fake_post,
    )
    assert captured["strict"] is False


def test_install_or_route_forwards_strict_to_service(monkeypatch, pack_dir):
    captured = {}

    def fake_push(pack_dir, *, route, allow_duplicates, reembed, strict):
        captured["strict"] = strict
        return {"action": "ingested"}

    mod.install_or_route(
        pack_dir,
        strict=False,
        route_fn=lambda: CorpusWriteRoute("via_service", port=47950),
        push_fn=fake_push,
    )
    assert captured["strict"] is False
