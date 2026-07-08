"""Health endpoint tests (NXS-775)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentalloy.api.health_router import HealthChecker, HealthResponse


# Backward compat: no health_checker in app.state → returns healthy with no dep details.
def test_health_returns_200_healthy_without_lifespan(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "healthy"


# -------- service provenance block (build contract health-provenance-stamp) --------


def test_health_service_block_without_checker(client: TestClient) -> None:
    from agentalloy import __version__

    body = client.get("/health").json()
    assert body["service"]["version"] == __version__
    assert body["service"]["corpus_stamp"] is None


def test_health_service_block_with_real_store(app: FastAPI, corpus_dir: Path) -> None:
    from agentalloy import __version__
    from agentalloy.storage.skill_store import open_skill_store

    store = open_skill_store(str(corpus_dir / "agentalloy.duck"), read_only=True)
    try:
        app.state.health_checker = HealthChecker(store, MagicMock(), MagicMock(), "stub-embed")
        with TestClient(app) as c:
            body = c.get("/health").json()
    finally:
        store.close()
    assert body["service"]["version"] == __version__
    stamp = body["service"]["corpus_stamp"]
    assert isinstance(stamp, str) and len(stamp) == 64
    int(stamp, 16)  # hex digest


def test_corpus_stamp_stable_across_calls(corpus_dir: Path) -> None:
    from agentalloy.storage.skill_store import open_skill_store

    store = open_skill_store(str(corpus_dir / "agentalloy.duck"), read_only=True)
    try:
        checker = HealthChecker(store, MagicMock(), MagicMock(), "stub-embed")
        assert checker._corpus_stamp() == checker._corpus_stamp()  # pyright: ignore[reportPrivateUsage]
    finally:
        store.close()


def test_corpus_stamp_changes_when_active_set_changes(corpus_dir: Path) -> None:
    from agentalloy.ingest import FragmentRecord, ReviewRecord
    from agentalloy.ingest import _insert as ingest_insert  # pyright: ignore[reportPrivateUsage]
    from agentalloy.storage.skill_store import open_skill_store

    store = open_skill_store(str(corpus_dir / "agentalloy.duck"))
    try:
        checker = HealthChecker(store, MagicMock(), MagicMock(), "stub-embed")
        before = checker._corpus_stamp()  # pyright: ignore[reportPrivateUsage]
        ingest_insert(
            store,
            ReviewRecord(
                skill_id="stamp-probe-skill",
                canonical_name="Stamp Probe",
                category="engineering",
                skill_class="domain",
                domain_tags=["testing"],
                always_apply=False,
                phase_scope=[],
                category_scope=["framework"],
                author="test",
                change_summary="initial",
                raw_prose="stamp probe",
                fragments=[FragmentRecord(sequence=1, fragment_type="execution", content="probe")],
                tier=None,
            ),
            force=False,
        )
        after = checker._corpus_stamp()  # pyright: ignore[reportPrivateUsage]
    finally:
        store.close()
    assert before is not None and after is not None
    assert before != after


def test_corpus_stamp_none_when_store_unreachable() -> None:
    broken = MagicMock()
    broken.execute.side_effect = RuntimeError("db locked")
    checker = HealthChecker(broken, MagicMock(), MagicMock(), "stub-embed")
    assert checker._corpus_stamp() is None  # pyright: ignore[reportPrivateUsage]


def _mock_checker(
    store_ok: bool = True,
    tel_ok: bool = True,
    embed_ok: bool = True,
    assemble_ok: bool = True,
) -> MagicMock:
    checker = MagicMock(spec=HealthChecker)

    async def _check() -> HealthResponse:
        from agentalloy.api.health_router import DependencyStatus

        def dep(ok: bool, impact: str) -> DependencyStatus:
            return DependencyStatus(
                status="ok" if ok else "unavailable",
                impact=None if ok else impact,
                detail=None if ok else "simulated failure",
            )

        deps = {
            "runtime_store": dep(store_ok, "compose and retrieve requests will fail"),
            "telemetry_store": dep(tel_ok, "trace persistence degraded"),
            "embedding_runtime": dep(embed_ok, "semantic retrieve will fail"),
            "runtime_cache": dep(assemble_ok, "compose requests will fail"),
        }
        if not store_ok:
            overall = "unavailable"
        elif not embed_ok or not assemble_ok or not tel_ok:
            overall = "degraded"
        else:
            overall = "healthy"
        return HealthResponse(status=overall, dependencies=deps)  # type: ignore[arg-type]

    checker.check = _check
    return checker


@pytest.fixture
def client_with_checker(app: FastAPI) -> TestClient:
    app.state.health_checker = _mock_checker()
    with TestClient(app) as c:
        return c


# AC-1: all deps available → healthy
def test_all_deps_available_reports_healthy(app: FastAPI) -> None:
    app.state.health_checker = _mock_checker()
    with TestClient(app) as c:
        resp = c.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "healthy"
    assert all(v["status"] == "ok" for v in body["dependencies"].values())


# AC-2: runtime store unavailable → unavailable with impact
def test_runtime_store_unavailable_reports_unavailable(app: FastAPI) -> None:
    app.state.health_checker = _mock_checker(store_ok=False)
    with TestClient(app) as c:
        resp = c.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "unavailable"
    dep = body["dependencies"]["runtime_store"]
    assert dep["status"] == "unavailable"
    assert dep["impact"] is not None and "compose" in dep["impact"]


# AC-3: embedding runtime unavailable → degraded
def test_embedding_runtime_unavailable_reports_degraded(app: FastAPI) -> None:
    app.state.health_checker = _mock_checker(embed_ok=False)
    with TestClient(app) as c:
        resp = c.get("/health")
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["dependencies"]["embedding_runtime"]["status"] == "unavailable"
    assert body["dependencies"]["embedding_runtime"]["impact"] is not None


# AC-4: telemetry store unavailable → degraded, other deps still ok
def test_telemetry_unavailable_does_not_imply_runtime_failure(app: FastAPI) -> None:
    app.state.health_checker = _mock_checker(tel_ok=False)
    with TestClient(app) as c:
        resp = c.get("/health")
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["dependencies"]["telemetry_store"]["status"] == "unavailable"
    assert body["dependencies"]["runtime_store"]["status"] == "ok"
    assert body["dependencies"]["embedding_runtime"]["status"] == "ok"


# #9 (§D): /health probes the Stage B reranker via the scorer's rolling outcome
# window. A timeout-dominant window → reranker "unavailable" and overall degraded
# (Stage B fails open — never "unavailable"). All other deps are mocked healthy.
def _real_checker_with_healthy_deps() -> HealthChecker:
    return HealthChecker(MagicMock(), MagicMock(), MagicMock(), "stub-embed")


def test_health_reranker_degraded_when_timeout_dominant(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    import agentalloy.retrieval.lm_assist as lm_assist
    from agentalloy.retrieval.lm_assist import LMAssistOutcome

    monkeypatch.setenv("LM_ASSIST", "arbitrate")
    lm_assist.reset_outcome_window()
    for _ in range(10):
        lm_assist._record_outcome(LMAssistOutcome.TIMEOUT)  # pyright: ignore[reportPrivateUsage]
    try:
        resp = asyncio.run(_real_checker_with_healthy_deps().check())
    finally:
        lm_assist.reset_outcome_window()

    assert resp.dependencies is not None
    reranker = resp.dependencies["reranker"]
    assert reranker.status == "unavailable"
    assert reranker.impact is not None and "Stage B" in reranker.impact
    assert resp.status == "degraded"  # degraded, never unavailable (fails open)


def test_health_reranker_ok_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    import agentalloy.retrieval.lm_assist as lm_assist
    from agentalloy.retrieval.lm_assist import LMAssistOutcome

    # Even with timeouts in the window, a disabled Stage B never reports unhealthy.
    monkeypatch.setenv("LM_ASSIST", "off")
    lm_assist.reset_outcome_window()
    for _ in range(10):
        lm_assist._record_outcome(LMAssistOutcome.TIMEOUT)  # pyright: ignore[reportPrivateUsage]
    try:
        resp = asyncio.run(_real_checker_with_healthy_deps().check())
    finally:
        lm_assist.reset_outcome_window()

    assert resp.dependencies is not None
    assert resp.dependencies["reranker"].status == "ok"
    assert resp.status == "healthy"


def test_health_upstream_not_configured_is_visible_but_not_degrading() -> None:
    import asyncio

    resp = asyncio.run(_real_checker_with_healthy_deps().check())

    assert resp.dependencies is not None
    upstream = resp.dependencies["upstream_llm"]
    assert upstream.status == "not_configured"
    assert upstream.impact is not None and "UPSTREAM_URL" in upstream.impact
    # Per-repo upstreams are a valid deployment; overall stays healthy.
    assert resp.status == "healthy"


def test_health_upstream_configured_reports_ok() -> None:
    import asyncio

    checker = HealthChecker(
        MagicMock(),
        MagicMock(),
        MagicMock(),
        "stub-embed",
        upstream_summary="url=http://h:9000 model=qwen",
    )
    resp = asyncio.run(checker.check())

    assert resp.dependencies is not None
    upstream = resp.dependencies["upstream_llm"]
    assert upstream.status == "ok"
    assert upstream.detail == "url=http://h:9000 model=qwen"
    assert resp.status == "healthy"
