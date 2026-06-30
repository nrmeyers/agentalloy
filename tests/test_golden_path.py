"""NXS-784: end-to-end golden path verification for Skill API v1.

Exercises the full v1 workflow in a single pass:
  - health endpoint
  - runtime diagnostics
  - compose (≥2 source skills, system skills included)
  - direct retrieve by ID
  - semantic retrieve (POST /retrieve)
  - skill inspection (GET /skills)
  - trace written to telemetry store

Skipped unless Ollama is reachable with both embedding and assembly models.

Run with:
    pytest -m integration tests/test_golden_path.py -v
"""

from __future__ import annotations

import time

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentalloy.api.compose_router import get_orchestrator
from agentalloy.api.diagnostics_router import DiagnosticsChecker
from agentalloy.api.health_router import HealthChecker
from agentalloy.api.retrieve_router import get_retrieve_orchestrator
from agentalloy.api.skill_router import get_skill_store
from agentalloy.app import create_app
from agentalloy.fixtures.loader import load_fixtures
from agentalloy.lm_client import OpenAICompatClient
from agentalloy.orchestration.compose import ComposeOrchestrator
from agentalloy.orchestration.retrieve import RetrieveOrchestrator
from agentalloy.runtime_state import load_runtime_cache
from agentalloy.storage.fragment_store import LanceFragmentStore
from agentalloy.storage.skill_store import DuckDBSkillStore, open_skill_store
from agentalloy.storage.telemetry_store import open_telemetry_store
from agentalloy.telemetry.writer import DuckDBTelemetryWriter

pytestmark = pytest.mark.integration

LM_BASE = "http://localhost:11434"
EMBED_MODEL = "nomic-embed-text-v1.5"

GOLDEN_TASK = (
    "Design a Python FastAPI endpoint that validates a JSON request body "
    "and returns a structured error response"
)
GOLDEN_PHASE = "design"


# ---------------------------------------------------------------------------
# Skip helpers
# ---------------------------------------------------------------------------


def _embed_model_responds(model: str) -> bool:
    """FastFlowLM hides the embedding slot from /v1/models — probe via
    /v1/embeddings directly."""
    try:
        r = httpx.post(
            f"{LM_BASE}/v1/embeddings",
            json={"model": model, "input": ["health"]},
            timeout=8.0,
        )
        if r.status_code != 200:
            return False
        body = r.json()
        return bool(body.get("data") and body["data"][0].get("embedding"))
    except httpx.HTTPError:
        return False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def seeded_store(tmp_path_factory: pytest.TempPathFactory) -> DuckDBSkillStore:
    if not _embed_model_responds(EMBED_MODEL):
        pytest.skip(f"FastFlowLM embed model {EMBED_MODEL} not responding")
    tmp = tmp_path_factory.mktemp("golden")
    store = open_skill_store(str(tmp / "agentalloy.duck"))
    store.migrate()
    summary = load_fixtures(store)
    assert summary.skills > 0, "fixture loader must seed at least one skill"
    return store


@pytest.fixture(scope="module")
def golden_app(seeded_store: DuckDBSkillStore, tmp_path_factory: pytest.TempPathFactory) -> FastAPI:
    if not _embed_model_responds(EMBED_MODEL):
        pytest.skip(f"FastFlowLM embed model {EMBED_MODEL} not responding")

    tmp = tmp_path_factory.mktemp("golden_tel")

    lm = OpenAICompatClient(LM_BASE)
    runtime = load_runtime_cache(seeded_store)
    # v5 splits the old conflated store: Lance for fragment search, a separate
    # telemetry.duck for composition traces.
    fragment_store = LanceFragmentStore(tmp / "fragments.lance")
    telemetry_store = open_telemetry_store(tmp / "telemetry.duck")
    telemetry = DuckDBTelemetryWriter(telemetry_store)

    # Populate the Lance fragments dataset for the loaded corpus so retrieve
    # has something to rank. Mirrors the reembed CLI inline.
    from agentalloy.reads import get_active_fragments
    from agentalloy.storage.protocols import FragmentEmbedding

    fragments = get_active_fragments(seeded_store)
    if fragments:
        contents = [f.content for f in fragments]
        vectors = lm.embed(model=EMBED_MODEL, texts=contents)
        now = int(time.time())
        fragment_store.insert_embeddings(
            [
                FragmentEmbedding(
                    fragment_id=f.fragment_id,
                    embedding=vec,
                    skill_id=f.skill_id,
                    category=f.category,
                    fragment_type=f.fragment_type,
                    embedded_at=now,
                    embedding_model=EMBED_MODEL,
                    prose=f.content,
                )
                for f, vec in zip(fragments, vectors, strict=True)
            ]
        )
        fragment_store.rebuild_fts_index()  # BM25 leg + fallback path need the FTS index

    compose_orch = ComposeOrchestrator(
        runtime,
        lm,
        fragment_store,
        telemetry,
        embedding_model=EMBED_MODEL,
    )
    retrieve_orch = RetrieveOrchestrator(
        runtime, lm, fragment_store, telemetry, embedding_model=EMBED_MODEL
    )
    health_checker = HealthChecker(
        seeded_store,
        lm,
        telemetry_store,
        EMBED_MODEL,
        runtime_load_error=None,
    )
    diagnostics_checker = DiagnosticsChecker(seeded_store, runtime, health_checker)

    app = create_app(use_default_lifespan=False)
    app.dependency_overrides[get_orchestrator] = lambda: compose_orch
    app.dependency_overrides[get_retrieve_orchestrator] = lambda: retrieve_orch
    app.dependency_overrides[get_skill_store] = lambda: seeded_store
    app.state.health_checker = health_checker
    app.state.diagnostics_checker = diagnostics_checker
    app.state.telemetry_store = telemetry_store
    return app


# ---------------------------------------------------------------------------
# Golden path tests — run sequentially, each building on the previous
# ---------------------------------------------------------------------------


def test_health_reports_healthy(golden_app: FastAPI) -> None:
    """Service must report healthy with all deps ok before we start."""
    with TestClient(golden_app) as c:
        resp = c.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] in ("healthy", "degraded"), f"unexpected status: {body['status']}"
    deps = body["dependencies"]
    assert deps["runtime_store"]["status"] == "ok"
    assert deps["embedding_runtime"]["status"] == "ok"


def test_diagnostics_cache_loaded_and_consistent(golden_app: FastAPI) -> None:
    """Runtime cache must be loaded and consistent with the store."""
    with TestClient(golden_app) as c:
        resp = c.get("/diagnostics/runtime")
    assert resp.status_code == 200
    body = resp.json()
    assert body["cache_loaded"] is True, "runtime cache must be loaded"
    assert body["consistency"]["consistent"] is True, f"cache/store mismatch: {body['consistency']}"
    assert len(body["store_state"]) > 0


def test_compose_returns_concatenated_fragments(golden_app: FastAPI) -> None:
    """POST /compose must return raw concatenated fragment text from source skills."""
    with TestClient(golden_app) as c:
        resp = c.post("/compose", json={"task": GOLDEN_TASK, "phase": GOLDEN_PHASE})

    assert resp.status_code == 200, f"compose failed: {resp.text}"
    body = resp.json()
    assert body["result_type"] == "composed", (
        f"expected composed result, got: {body['result_type']}"
    )
    assert body["output"], "compose output must not be empty"
    assert len(body["source_skills"]) >= 1, (
        f"expected ≥1 source skill, got: {body['source_skills']}"
    )
    # v5.4: no LLM in compose path
    assert body["assembly_tier"] == 0
    assert body["latency_ms"]["assembly_ms"] == 0


def test_compose_includes_system_skills(golden_app: FastAPI) -> None:
    """Compose must automatically include applicable system skills."""
    with TestClient(golden_app) as c:
        resp = c.post("/compose", json={"task": GOLDEN_TASK, "phase": GOLDEN_PHASE})
    assert resp.status_code == 200
    body = resp.json()
    assert body["system_skills_applied"] is True, "system skills must be applied"
    assert len(body["system_fragments"]) > 0, "system_fragments must be non-empty"


def test_retrieve_by_id_returns_active_skill(
    golden_app: FastAPI, seeded_store: DuckDBSkillStore
) -> None:
    """GET /retrieve/{skill_id} must return active skill content."""
    rows = seeded_store.execute(
        "SELECT skill_id FROM skills WHERE current_version_id IS NOT NULL LIMIT 1"
    )
    assert rows, "seeded store must have at least one active skill"
    skill_id = str(rows[0][0])

    with TestClient(golden_app) as c:
        resp = c.get(f"/retrieve/{skill_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["skill_id"] == skill_id
    assert body["active_version"]["version_id"]
    assert body["raw_prose"]


def test_semantic_retrieve_returns_ranked_hits(golden_app: FastAPI) -> None:
    """POST /retrieve must return ranked skill hits for the golden task."""
    with TestClient(golden_app) as c:
        resp = c.post(
            "/retrieve",
            json={"task": GOLDEN_TASK, "phase": GOLDEN_PHASE, "k": 3},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["results"]) > 0, "semantic retrieve must return at least one hit"
    hit = body["results"][0]
    assert hit["skill_id"]
    assert hit["score"] > 0
    assert hit["raw_prose"]


def test_skill_inspection_returns_full_detail(
    golden_app: FastAPI, seeded_store: DuckDBSkillStore
) -> None:
    """GET /skills/{skill_id} must return active version detail and fragments."""
    rows = seeded_store.execute(
        "SELECT skill_id FROM skills WHERE current_version_id IS NOT NULL LIMIT 1"
    )
    skill_id = str(rows[0][0])

    with TestClient(golden_app) as c:
        resp = c.get(f"/skills/{skill_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["skill_id"] == skill_id
    assert body["active_version"]["raw_prose"]
    assert len(body["fragments"]) > 0


def test_compose_trace_written_to_telemetry(golden_app: FastAPI) -> None:
    """After compose, the telemetry store must contain a trace with all required fields."""
    with TestClient(golden_app) as c:
        c.post("/compose", json={"task": GOLDEN_TASK, "phase": GOLDEN_PHASE})

    # Brief pause to let the background drain thread flush.
    time.sleep(0.2)

    telemetry_store = golden_app.state.telemetry_store
    traces = telemetry_store.query_traces(status="compose", limit=1)

    assert traces, "no compose trace found in telemetry store"
    t = traces[0]

    assert t.status == "compose"
    assert t.task_prompt == GOLDEN_TASK
    assert t.source_skill_ids and len(t.source_skill_ids) >= 1, (
        f"source_skill_ids must have ≥1 entries, got: {t.source_skill_ids}"
    )
    assert t.selected_fragment_ids, "selected_fragment_ids must be non-empty"
    assert t.assembly_tier == "0", f"assembly_tier must be '0' (no LLM), got: {t.assembly_tier!r}"
    assert t.retrieval_latency_ms is not None and t.retrieval_latency_ms >= 0
    assert t.assembly_latency_ms == 0
    assert t.total_latency_ms is not None and t.total_latency_ms >= 0
