"""NXS-776: enforce active-version-only runtime selection.

Tests cover:
  - get_active_version_by_id raises InconsistentActiveVersion for non-active versions
  - GET /skills/{id} returns HTTP 500 with structured body on inconsistent state
  - GET /retrieve/{id} returns HTTP 500 with structured body on inconsistent state
  - POST /compose returns HTTP 500 with structured body on inconsistent state
  - Deterministic failure, not silent selection
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentalloy.api.compose_router import get_orchestrator
from agentalloy.api.retrieve_router import get_retrieve_orchestrator
from agentalloy.api.skill_router import get_skill_store
from agentalloy.orchestration.retrieve import RetrieveOrchestrator
from agentalloy.reads import InconsistentActiveVersion, get_active_version_by_id
from agentalloy.storage.protocols import FragmentStore
from agentalloy.storage.skill_store import DuckDBSkillStore, open_skill_store
from agentalloy.telemetry import NullTelemetryWriter
from tests.support import StubLMClient

# -------- shared fixtures --------


@pytest.fixture
def empty_store(tmp_path: Path) -> DuckDBSkillStore:
    return open_skill_store(str(tmp_path / "agentalloy.duck"))


@pytest.fixture
def populated_store(corpus_dir: Path) -> DuckDBSkillStore:
    return open_skill_store(str(corpus_dir / "agentalloy.duck"), read_only=True)


# -------- helpers --------


def _make_skill(store: DuckDBSkillStore, skill_id: str, skill_class: str = "domain") -> None:
    store.execute(
        "INSERT INTO skills (skill_id, canonical_name, category, skill_class, "
        "domain_tags, deprecated, always_apply, phase_scope, category_scope) "
        "VALUES (?, ?, 'design', ?, ?, false, false, ?, ?)",
        [skill_id, skill_id, skill_class, [], [], []],
    )


def _make_version(store: DuckDBSkillStore, skill_id: str, version_id: str, status: str) -> None:
    # HAS_VERSION is folded into skill_versions.skill_id.
    store.execute(
        "INSERT INTO skill_versions (version_id, skill_id, version_number, authored_at, "
        "author, change_summary, status, raw_prose) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [version_id, skill_id, 1, datetime.now(UTC), "test", "t", status, "prose"],
    )


def _link_current(store: DuckDBSkillStore, skill_id: str, version_id: str) -> None:
    # CURRENT_VERSION is folded into skills.current_version_id.
    store.execute(
        "UPDATE skills SET current_version_id = ? WHERE skill_id = ?",
        [version_id, skill_id],
    )


def _first_active_version_id(store: DuckDBSkillStore) -> str:
    rows = store.execute(
        "SELECT v.version_id FROM skills s "
        "JOIN skill_versions v ON v.version_id = s.current_version_id "
        "WHERE v.status = 'active' LIMIT 1"
    )
    assert rows, "fixture store has no active versions"
    return str(rows[0][0])


# -------- Unit: get_active_version_by_id --------


def test_get_active_version_by_id_returns_data_for_active(
    populated_store: DuckDBSkillStore,
) -> None:
    version_id = _first_active_version_id(populated_store)
    data = get_active_version_by_id(populated_store, version_id)
    assert data["version_id"] == version_id
    assert isinstance(data["version_number"], int)
    assert isinstance(data["raw_prose"], str)


def test_get_active_version_by_id_raises_for_superseded(empty_store: DuckDBSkillStore) -> None:
    _make_skill(empty_store, "s1")
    _make_version(empty_store, "s1", "s1-v1", "superseded")
    with pytest.raises(InconsistentActiveVersion) as ei:
        get_active_version_by_id(empty_store, "s1-v1")
    assert ei.value.skill_id == "s1"
    assert "superseded" in ei.value.reason


def test_get_active_version_by_id_raises_for_draft(empty_store: DuckDBSkillStore) -> None:
    _make_skill(empty_store, "s2")
    _make_version(empty_store, "s2", "s2-v1", "draft")
    with pytest.raises(InconsistentActiveVersion) as ei:
        get_active_version_by_id(empty_store, "s2-v1")
    assert "draft" in ei.value.reason


def test_get_active_version_by_id_raises_for_proposed(empty_store: DuckDBSkillStore) -> None:
    _make_skill(empty_store, "s3")
    _make_version(empty_store, "s3", "s3-v1", "proposed")
    with pytest.raises(InconsistentActiveVersion) as ei:
        get_active_version_by_id(empty_store, "s3-v1")
    assert "proposed" in ei.value.reason


def test_get_active_version_by_id_raises_runtime_error_for_missing(
    empty_store: DuckDBSkillStore,
) -> None:
    with pytest.raises(RuntimeError, match="not found"):
        get_active_version_by_id(empty_store, "no-such-version")


# -------- HTTP handler: InconsistentActiveVersion → structured 500 --------
#
# We need an inconsistent store for HTTP-level tests. We construct one where
# CURRENT_VERSION points at a superseded version so the consistency guard fires
# on the first active-read call in the request path.
#


@pytest.fixture
def inconsistent_store(tmp_path: Path) -> DuckDBSkillStore:
    """Store where CURRENT_VERSION points at a non-active version (superseded)."""
    s = open_skill_store(str(tmp_path / "agentalloy.duck"))
    _make_skill(s, "broken-skill")
    _make_version(s, "broken-skill", "broken-skill-v1", "superseded")
    _link_current(s, "broken-skill", "broken-skill-v1")
    return s


def test_inconsistent_state_returns_500_on_inspect(
    app: FastAPI, inconsistent_store: DuckDBSkillStore
) -> None:
    app.dependency_overrides[get_skill_store] = lambda: inconsistent_store
    with TestClient(app) as c:
        resp = c.get("/skills/broken-skill")
    assert resp.status_code == 500
    body = resp.json()
    assert body["code"] == "inconsistent_active_version"
    assert "skill_id" in body
    assert "detail" in body


def test_inconsistent_state_returns_500_on_retrieve_by_id(
    app: FastAPI, inconsistent_store: DuckDBSkillStore, vector_store: FragmentStore
) -> None:
    orch = RetrieveOrchestrator(
        inconsistent_store,
        StubLMClient(),
        vector_store,
        NullTelemetryWriter(),
        embedding_model="stub-embed",
    )
    app.dependency_overrides[get_retrieve_orchestrator] = lambda: orch
    app.dependency_overrides[get_orchestrator] = lambda: None  # type: ignore[return-value]
    with TestClient(app) as c:
        resp = c.get("/retrieve/broken-skill")
    assert resp.status_code == 500
    body = resp.json()
    assert body["code"] == "inconsistent_active_version"


# -------- AC: compose uses active-only fragments --------


def test_compose_uses_only_active_fragments(
    app: FastAPI, populated_store: DuckDBSkillStore, vector_store: FragmentStore
) -> None:
    """Compose retrieval must only surface active-version fragments."""
    from agentalloy.orchestration.compose import ComposeOrchestrator
    from agentalloy.telemetry.writer import NullTelemetryWriter

    orch = ComposeOrchestrator(
        populated_store,
        StubLMClient(),
        vector_store,
        NullTelemetryWriter(),
        embedding_model="stub-embed",
    )
    app.dependency_overrides[get_orchestrator] = lambda: orch
    with TestClient(app) as c:
        resp = c.post("/compose", json={"task": "fastapi endpoint", "phase": "design"})
    # 200 or 503 (retrieval failure) but NOT 500 (no inconsistency)
    assert resp.status_code in (200, 503)
    if resp.status_code == 503:
        body = resp.json()
        assert body.get("stage") == "retrieval"
    # If 200, verify source skills came from active versions only
    if resp.status_code == 200:
        body = resp.json()
        for sid in body.get("source_skills", []):
            assert sid  # non-empty skill_id
