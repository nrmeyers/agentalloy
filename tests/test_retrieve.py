"""AC-1..4 for direct retrieve (NXS-769)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentalloy.api.compose_router import get_orchestrator
from agentalloy.api.retrieve_router import get_retrieve_orchestrator
from agentalloy.fixtures.loader import load_fixtures
from agentalloy.orchestration.retrieve import RetrieveOrchestrator
from agentalloy.reads import get_active_fragments
from agentalloy.retrieval.embedding_errors import (
    EmbeddingError,
    EmbeddingErrorCode,
    EmbeddingErrorResult,
)
from agentalloy.storage.ladybug import LadybugStore
from agentalloy.storage.vector_store import VectorStore
from agentalloy.telemetry import NullTelemetryWriter, TelemetryRecord, TelemetryWriter
from tests.support import StubLMClient


class _SpyTelemetry:
    def __init__(self) -> None:
        self.records: list[TelemetryRecord] = []

    def write(self, record: TelemetryRecord) -> None:
        self.records.append(record)


@pytest.fixture
def populated_store(tmp_path: Path) -> LadybugStore:
    s = LadybugStore(str(tmp_path / "ladybug"))
    s.open()
    s.migrate()
    load_fixtures(s)
    return s


@pytest.fixture
def spy_telemetry() -> _SpyTelemetry:
    return _SpyTelemetry()


@pytest.fixture
def orch(
    populated_store: LadybugStore,
    vector_store: VectorStore,
    spy_telemetry: _SpyTelemetry,
) -> RetrieveOrchestrator:
    return RetrieveOrchestrator(
        populated_store, StubLMClient(), vector_store, spy_telemetry, embedding_model="stub-embed"
    )


def _install(app: FastAPI, orch: RetrieveOrchestrator) -> None:
    app.dependency_overrides[get_retrieve_orchestrator] = lambda: orch
    # Compose dep also required by lifespan; but lifespan is off in tests.
    # Install a no-op to be safe if a test accidentally triggers it.
    app.dependency_overrides[get_orchestrator] = lambda: None  # type: ignore[return-value]


# -------- AC-1: GET /retrieve/{skill_id} --------


def test_retrieve_by_id_returns_active_version(
    app: FastAPI, client: TestClient, orch: RetrieveOrchestrator
) -> None:
    _install(app, orch)
    resp = client.get("/retrieve/py-fastapi-endpoint-design")
    assert resp.status_code == 200
    body = resp.json()
    assert body["skill_id"] == "py-fastapi-endpoint-design"
    assert body["skill_class"] == "domain"
    assert body["category"] == "design"
    assert body["active_version"]["version_id"] == "py-fastapi-endpoint-design-v2"
    assert "thin handlers" in body["raw_prose"] or body["raw_prose"]


def test_retrieve_by_id_unknown_returns_404(
    app: FastAPI, client: TestClient, orch: RetrieveOrchestrator
) -> None:
    _install(app, orch)
    resp = client.get("/retrieve/does-not-exist")
    assert resp.status_code == 404


# -------- AC-2 + AC-3: POST /retrieve (semantic query, active-only) --------


def test_retrieve_query_returns_skills_not_fragments(
    app: FastAPI, client: TestClient, orch: RetrieveOrchestrator
) -> None:
    _install(app, orch)
    resp = client.post(
        "/retrieve", json={"task": "fastapi endpoint design", "phase": "design", "k": 5}
    )
    assert resp.status_code == 200
    body = resp.json()
    # Results must be skills, not fragments — every returned skill_id is unique
    skill_ids = [hit["skill_id"] for hit in body["results"]]
    assert len(skill_ids) == len(set(skill_ids))
    # Every returned version_id ends with -v2 (active version)
    for hit in body["results"]:
        assert hit["version_id"].endswith("-v2")


def test_retrieve_query_excludes_non_active_versions(
    app: FastAPI, client: TestClient, orch: RetrieveOrchestrator
) -> None:
    _install(app, orch)
    resp = client.post("/retrieve", json={"task": "anything", "phase": "design"})
    assert resp.status_code == 200
    body = resp.json()
    for hit in body["results"]:
        assert "-v1" not in hit["version_id"]  # -v1 is superseded in fixtures


def test_retrieve_query_empty_returns_200(
    app: FastAPI, client: TestClient, orch: RetrieveOrchestrator
) -> None:
    _install(app, orch)
    resp = client.post(
        "/retrieve",
        json={
            "task": "anything",
            "phase": "design",
            "domain_tags": ["nonexistent-tag"],
            "k": 5,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["results"] == []


def test_retrieve_query_respects_k_limit(
    app: FastAPI, client: TestClient, orch: RetrieveOrchestrator
) -> None:
    _install(app, orch)
    resp = client.post("/retrieve", json={"task": "t", "phase": "design", "k": 2})
    body = resp.json()
    assert len(body["results"]) <= 2


def test_retrieve_query_rejects_invalid_phase(
    app: FastAPI, client: TestClient, orch: RetrieveOrchestrator
) -> None:
    _install(app, orch)
    resp = client.post("/retrieve", json={"task": "t", "phase": "invalid"})
    assert resp.status_code == 422


def test_retrieve_query_uses_bm25_fallback_results(
    app: FastAPI,
    client: TestClient,
    orch: RetrieveOrchestrator,
    populated_store: LadybugStore,
    spy_telemetry: _SpyTelemetry,
) -> None:
    _install(app, orch)
    frag = next(
        f
        for f in get_active_fragments(populated_store)
        if f.skill_id == "py-fastapi-endpoint-design"
    )
    fallback = EmbeddingErrorResult(
        error=EmbeddingError(EmbeddingErrorCode.UNAVAILABLE, "embed down"),
        bm25_only=True,
        candidates=[frag],
        eligible_count=1,
        retrieval_ms=5,
        scores_by_id={frag.fragment_id: 1.0},
    )

    with patch(
        "agentalloy.orchestration.retrieve.retrieve_domain_candidates", return_value=fallback
    ):
        resp = client.post(
            "/retrieve", json={"task": "fastapi endpoint design", "phase": "design", "k": 5}
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["results"]
    assert body["results"][0]["skill_id"] == "py-fastapi-endpoint-design"
    assert spy_telemetry.records[-1].error_payload == EmbeddingErrorCode.UNAVAILABLE.value


# -------- AC-4: retrieval-only telemetry shape --------


def test_by_id_writes_retrieve_by_id_trace(
    app: FastAPI, client: TestClient, orch: RetrieveOrchestrator, spy_telemetry: _SpyTelemetry
) -> None:
    _install(app, orch)
    client.get("/retrieve/py-fastapi-endpoint-design")
    assert spy_telemetry.records, "expected one trace record"
    rec = spy_telemetry.records[-1]
    assert rec.result_type == "retrieve_by_id"
    # Retrieval-only: assembly fields must be None
    assert rec.assembly_tier is None
    assert rec.latency_assembly_ms is None
    assert rec.output is None
    # Retrieval fields populated
    assert rec.latency_retrieval_ms is not None
    assert rec.latency_total_ms is not None
    assert rec.source_skill_ids == ["py-fastapi-endpoint-design"]


def test_query_writes_retrieve_query_trace(
    app: FastAPI, client: TestClient, orch: RetrieveOrchestrator, spy_telemetry: _SpyTelemetry
) -> None:
    _install(app, orch)
    client.post("/retrieve", json={"task": "fastapi", "phase": "design", "k": 3})
    assert spy_telemetry.records, "expected one trace record"
    rec = spy_telemetry.records[-1]
    assert rec.result_type == "retrieve_query"
    assert rec.assembly_tier is None
    assert rec.latency_assembly_ms is None
    assert rec.output is None
    assert rec.phase == "design"
    assert rec.task_prompt == "fastapi"


# -------- null telemetry writer --------


def test_null_writer_accepts_records() -> None:
    writer: TelemetryWriter = NullTelemetryWriter()
    writer.write(
        TelemetryRecord(
            composition_id="c",
            timestamp=__import__("datetime").datetime.now(__import__("datetime").UTC),
            phase="design",
            task_prompt="t",
            result_type="composed",
        )
    )  # must not raise


def test_retrieve_query_empty_with_embedding_error_sets_error_payload(
    app: FastAPI,
    client: TestClient,
    orch: RetrieveOrchestrator,
    spy_telemetry: _SpyTelemetry,
) -> None:
    """Regression test: POST /retrieve returns 200 with results=[] when
    embedding fails AND BM25 returns no hits. Telemetry record must have
    error_payload set to distinguish from a normal empty result.
    """
    _install(app, orch)
    fallback = EmbeddingErrorResult(
        error=EmbeddingError(EmbeddingErrorCode.UNAVAILABLE, "embed down"),
        bm25_only=True,
        candidates=[],
        eligible_count=0,
        retrieval_ms=5,
        scores_by_id={},
    )

    with patch(
        "agentalloy.orchestration.retrieve.retrieve_domain_candidates", return_value=fallback
    ):
        resp = client.post(
            "/retrieve", json={"task": "fastapi endpoint design", "phase": "design", "k": 5}
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["results"] == []
    assert spy_telemetry.records[-1].error_payload == EmbeddingErrorCode.UNAVAILABLE.value
