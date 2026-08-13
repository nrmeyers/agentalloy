"""NXS-778: stale-content diagnostics — GET /diagnostics/runtime."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentalloy.api.diagnostics_router import (
    DiagnosticsChecker,
    SkillVersionEntry,
    compute_consistency,
)
from agentalloy.api.health_router import DependencyStatus, HealthChecker, HealthResponse
from agentalloy.reads.models import ActiveSkill
from agentalloy.runtime_state import RuntimeCache, VersionDetail, load_runtime_cache
from agentalloy.storage.skill_store import DuckDBSkillStore, open_skill_store

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def populated_store(corpus_dir: Path) -> DuckDBSkillStore:
    return open_skill_store(str(corpus_dir / "agentalloy.duck"), read_only=True)


@pytest.fixture
def loaded_cache(populated_store: DuckDBSkillStore) -> RuntimeCache:
    return load_runtime_cache(populated_store)


def _healthy_checker() -> MagicMock:
    checker = MagicMock(spec=HealthChecker)

    async def _check() -> HealthResponse:
        deps = {
            "runtime_store": DependencyStatus(status="ok"),
            "telemetry_store": DependencyStatus(status="ok"),
            "embedding_runtime": DependencyStatus(status="ok"),
            "runtime_cache": DependencyStatus(status="ok"),
        }
        return HealthResponse(status="healthy", dependencies=deps)  # type: ignore[arg-type]

    checker.check = _check
    return checker


def _degraded_checker(
    *, embed_ok: bool = True, assemble_ok: bool = True, tel_ok: bool = True, store_ok: bool = True
) -> MagicMock:
    checker = MagicMock(spec=HealthChecker)

    async def _check() -> HealthResponse:
        def dep(ok: bool, impact: str) -> DependencyStatus:
            return DependencyStatus(
                status="ok" if ok else "unavailable",
                impact=None if ok else impact,
                detail=None if ok else "simulated failure",
            )

        deps = {
            "runtime_store": dep(store_ok, "compose and retrieve will fail"),
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


def _stale_cache(populated_store: DuckDBSkillStore, stale_skill_id: str) -> RuntimeCache:
    """Build a RuntimeCache where one skill has a deliberately wrong version_id."""
    real = load_runtime_cache(populated_store)
    real_skill = real.get_active_skill_by_id(stale_skill_id)
    assert real_skill is not None
    stale_skill = ActiveSkill(
        skill_id=real_skill.skill_id,
        canonical_name=real_skill.canonical_name,
        category=real_skill.category,
        skill_class=real_skill.skill_class,
        domain_tags=real_skill.domain_tags,
        always_apply=real_skill.always_apply,
        phase_scope=real_skill.phase_scope,
        category_scope=real_skill.category_scope,
        active_version_id="stale-version-id-xyz",
        tier=real_skill.tier,
    )
    skills = {s.skill_id: s for s in real.get_active_skills()}
    skills[stale_skill_id] = stale_skill
    stale_version = VersionDetail(
        version_id="stale-version-id-xyz",
        version_number=999,
        authored_at=None,
        author="test",
        change_summary="stale",
        raw_prose="",
    )
    version_details = {
        s.active_version_id: real.get_version_detail(s.active_version_id)  # type: ignore[misc]
        for s in real.get_active_skills()
    }
    version_details["stale-version-id-xyz"] = stale_version
    return RuntimeCache(
        skills=skills,
        fragments=real.get_active_fragments(),
        version_details=version_details,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# RuntimeCache unit tests
# ---------------------------------------------------------------------------


def test_runtime_cache_loads_from_store(populated_store: DuckDBSkillStore) -> None:
    cache = load_runtime_cache(populated_store)
    skills = cache.get_active_skills()
    assert len(skills) > 0
    for s in skills:
        assert s.skill_id
        assert s.active_version_id
        assert s.canonical_name


def test_runtime_cache_entries_match_store_active_versions(
    populated_store: DuckDBSkillStore,
    loaded_cache: RuntimeCache,
) -> None:
    """Cache version_ids should match what the store reports."""
    rows = populated_store.execute(
        "SELECT s.skill_id, v.version_id FROM skills s "
        "JOIN skill_versions v ON v.version_id = s.current_version_id "
        "WHERE v.status = 'active' AND s.deprecated = false"
    )
    store_map = {str(r[0]): str(r[1]) for r in rows}
    cache_map = {s.skill_id: s.active_version_id for s in loaded_cache.get_active_skills()}
    assert cache_map == store_map


# ---------------------------------------------------------------------------
# Consistency computation unit tests
# ---------------------------------------------------------------------------


def _entry(skill_id: str, version_id: str) -> SkillVersionEntry:
    return SkillVersionEntry(skill_id=skill_id, canonical_name="n/a", version_id=version_id)


def test_consistency_all_match() -> None:
    store = [_entry("s1", "v1"), _entry("s2", "v2")]
    cache = [_entry("s1", "v1"), _entry("s2", "v2")]
    report = compute_consistency(store, cache)
    assert report.consistent is True
    assert sorted(report.matched) == ["s1", "s2"]
    assert not report.missing_in_cache
    assert not report.missing_in_store
    assert not report.version_mismatches


def test_consistency_missing_in_cache() -> None:
    store = [_entry("s1", "v1"), _entry("s2", "v2")]
    cache = [_entry("s1", "v1")]
    report = compute_consistency(store, cache)
    assert report.consistent is False
    assert report.missing_in_cache == ["s2"]
    assert not report.missing_in_store


def test_consistency_missing_in_store() -> None:
    store = [_entry("s1", "v1")]
    cache = [_entry("s1", "v1"), _entry("s2", "v2")]
    report = compute_consistency(store, cache)
    assert report.consistent is False
    assert report.missing_in_store == ["s2"]
    assert not report.missing_in_cache


def test_consistency_version_mismatch() -> None:
    store = [_entry("s1", "v1")]
    cache = [_entry("s1", "v999")]
    report = compute_consistency(store, cache)
    assert report.consistent is False
    assert len(report.version_mismatches) == 1
    mm = report.version_mismatches[0]
    assert mm.skill_id == "s1"
    assert mm.store_version_id == "v1"
    assert mm.cache_version_id == "v999"


# ---------------------------------------------------------------------------
# DiagnosticsChecker integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_diagnostics_consistent_when_cache_matches_store(
    populated_store: DuckDBSkillStore,
    loaded_cache: RuntimeCache,
) -> None:
    """AC-1/AC-2: cache loaded from same store → fully consistent."""
    checker = DiagnosticsChecker(populated_store, loaded_cache, _healthy_checker())
    result = await checker.check()
    assert result.cache_loaded is True
    assert result.consistency.consistent is True
    assert len(result.store_state) > 0
    assert len(result.runtime_state) > 0
    assert len(result.consistency.matched) == len(result.store_state)


@pytest.mark.asyncio
async def test_diagnostics_detects_stale_cache(
    populated_store: DuckDBSkillStore,
) -> None:
    """AC-1/AC-3: deliberately stale cache → mismatch flagged."""
    real_skill_rows = populated_store.execute(
        "SELECT s.skill_id FROM skills s "
        "JOIN skill_versions v ON v.version_id = s.current_version_id "
        "WHERE v.status = 'active' LIMIT 1"
    )
    assert real_skill_rows, "fixture store must have at least one active skill"
    skill_id = str(real_skill_rows[0][0])

    stale = _stale_cache(populated_store, skill_id)
    checker = DiagnosticsChecker(populated_store, stale, _healthy_checker())
    result = await checker.check()
    assert result.consistency.consistent is False
    assert any(mm.skill_id == skill_id for mm in result.consistency.version_mismatches)


@pytest.mark.asyncio
async def test_diagnostics_per_path_all_up_when_all_deps_ok(
    populated_store: DuckDBSkillStore,
    loaded_cache: RuntimeCache,
) -> None:
    """AC-4: all deps ok → all paths ready."""
    checker = DiagnosticsChecker(populated_store, loaded_cache, _healthy_checker())
    result = await checker.check()
    pp = result.dependency_readiness.per_path
    assert pp.compose is True
    assert pp.retrieve is True
    assert pp.inspect is True
    assert pp.telemetry is True


@pytest.mark.asyncio
async def test_diagnostics_per_path_embedding_down(
    populated_store: DuckDBSkillStore,
    loaded_cache: RuntimeCache,
) -> None:
    """AC-4: embedding down → compose and retrieve fail; inspect and telemetry unaffected."""
    checker = DiagnosticsChecker(populated_store, loaded_cache, _degraded_checker(embed_ok=False))
    result = await checker.check()
    pp = result.dependency_readiness.per_path
    assert pp.compose is False
    assert pp.retrieve is False
    assert pp.inspect is True
    assert pp.telemetry is True
    assert result.dependency_readiness.embedding_runtime == "unavailable"


@pytest.mark.asyncio
async def test_diagnostics_per_path_assembly_down(
    populated_store: DuckDBSkillStore,
    loaded_cache: RuntimeCache,
) -> None:
    """AC-4: assembly down → only compose fails."""
    checker = DiagnosticsChecker(
        populated_store, loaded_cache, _degraded_checker(assemble_ok=False)
    )
    result = await checker.check()
    pp = result.dependency_readiness.per_path
    assert pp.compose is False
    assert pp.retrieve is True
    assert pp.inspect is True
    assert pp.telemetry is True


@pytest.mark.asyncio
async def test_diagnostics_per_path_telemetry_down(
    populated_store: DuckDBSkillStore,
    loaded_cache: RuntimeCache,
) -> None:
    """AC-4: telemetry down → only telemetry path fails."""
    checker = DiagnosticsChecker(populated_store, loaded_cache, _degraded_checker(tel_ok=False))
    result = await checker.check()
    pp = result.dependency_readiness.per_path
    assert pp.compose is True
    assert pp.retrieve is True
    assert pp.inspect is True
    assert pp.telemetry is False


@pytest.mark.asyncio
async def test_diagnostics_per_path_store_down(
    populated_store: DuckDBSkillStore,
    loaded_cache: RuntimeCache,
) -> None:
    """AC-4: store down → compose, retrieve, inspect all fail."""
    checker = DiagnosticsChecker(populated_store, loaded_cache, _degraded_checker(store_ok=False))
    result = await checker.check()
    pp = result.dependency_readiness.per_path
    assert pp.compose is False
    assert pp.retrieve is False
    assert pp.inspect is False
    assert pp.telemetry is True


# ---------------------------------------------------------------------------
# HTTP endpoint tests
# ---------------------------------------------------------------------------


def test_diagnostics_endpoint_no_checker_returns_stub(client: TestClient) -> None:
    """Without a live checker in app.state, endpoint returns empty consistent stub."""
    resp = client.get("/diagnostics/runtime")
    assert resp.status_code == 200
    body = resp.json()
    assert body["cache_loaded"] is False
    assert body["consistency"]["consistent"] is True
    assert body["store_state"] == []
    assert body["runtime_state"] == []


def test_diagnostics_endpoint_with_checker(
    app: FastAPI,
    populated_store: DuckDBSkillStore,
    loaded_cache: RuntimeCache,
) -> None:
    """AC-1/AC-2: endpoint returns populated consistent diagnostics."""
    app.state.diagnostics_checker = DiagnosticsChecker(
        populated_store, loaded_cache, _healthy_checker()
    )
    with TestClient(app) as c:
        resp = c.get("/diagnostics/runtime")
    assert resp.status_code == 200
    body = resp.json()
    assert body["cache_loaded"] is True
    assert body["consistency"]["consistent"] is True
    assert len(body["store_state"]) > 0
    assert len(body["runtime_state"]) > 0
    dr = body["dependency_readiness"]
    assert dr["runtime_store"] == "ok"
    pp = dr["per_path"]
    assert all(pp[k] is True for k in ("compose", "retrieve", "inspect", "telemetry"))


def test_diagnostics_endpoint_stale_cache_detected(
    app: FastAPI,
    populated_store: DuckDBSkillStore,
) -> None:
    """AC-3: operator can distinguish stale cache via endpoint."""
    real_skill_rows = populated_store.execute(
        "SELECT s.skill_id FROM skills s "
        "JOIN skill_versions v ON v.version_id = s.current_version_id "
        "WHERE v.status = 'active' LIMIT 1"
    )
    skill_id = str(real_skill_rows[0][0])

    stale = _stale_cache(populated_store, skill_id)
    app.state.diagnostics_checker = DiagnosticsChecker(populated_store, stale, _healthy_checker())
    with TestClient(app) as c:
        resp = c.get("/diagnostics/runtime")
    assert resp.status_code == 200
    body = resp.json()
    assert body["consistency"]["consistent"] is False
    mismatches = body["consistency"]["version_mismatches"]
    assert any(m["skill_id"] == skill_id for m in mismatches)
