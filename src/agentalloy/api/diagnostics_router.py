"""Stale-content diagnostics endpoint — GET /diagnostics/runtime (NXS-778).

Exposes three slices of diagnostic information:
- store_state: active skill versions currently recorded in LadybugDB
- runtime_state: active skill versions loaded into the in-memory RuntimeCache
- consistency: whether store and cache agree (flags any mismatches)
- dependency_readiness: which dependencies are reachable, broken out per path
"""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from agentalloy.api.health_router import HealthChecker
from agentalloy.reads.active import get_active_skills
from agentalloy.reads.models import ActiveSkill
from agentalloy.runtime_state import RuntimeCache
from agentalloy.storage.ladybug import LadybugStore

router = APIRouter()

DepStatus = Literal["ok", "unavailable"]


class SkillVersionEntry(BaseModel):
    skill_id: str
    canonical_name: str
    version_id: str


class VersionMismatch(BaseModel):
    skill_id: str
    store_version_id: str
    cache_version_id: str


class ConsistencyReport(BaseModel):
    consistent: bool
    matched: list[str]
    """skill_ids where store and cache agree."""
    missing_in_cache: list[str]
    """skill_ids present in store but absent from cache."""
    missing_in_store: list[str]
    """skill_ids present in cache but absent from store."""
    version_mismatches: list[VersionMismatch]
    """skill_ids where both sides have an entry but version_id differs."""


class PathReadiness(BaseModel):
    compose: bool
    """Requires: runtime_store + embedding_runtime + runtime_cache."""
    retrieve: bool
    """Requires: runtime_store + embedding_runtime (telemetry degrades only)."""
    inspect: bool
    """Requires: runtime_store only."""
    telemetry: bool
    """Requires: telemetry_store only."""


class DependencyReadiness(BaseModel):
    runtime_store: DepStatus
    telemetry_store: DepStatus
    embedding_runtime: DepStatus
    runtime_cache: DepStatus
    per_path: PathReadiness


class RuntimeDiagnosticsResponse(BaseModel):
    cache_loaded: bool
    store_state: list[SkillVersionEntry]
    runtime_state: list[SkillVersionEntry]
    consistency: ConsistencyReport
    dependency_readiness: DependencyReadiness


class DiagnosticsChecker:
    def __init__(
        self,
        store: LadybugStore,
        cache: RuntimeCache | None,
        health_checker: HealthChecker,
    ) -> None:
        self._store = store
        self._cache = cache
        self._health_checker = health_checker

    async def check(self) -> RuntimeDiagnosticsResponse:
        # _read_store_state and health_checker.check() both touch the same
        # (non-thread-safe) LadybugStore connection, so run them sequentially
        # rather than concurrently to avoid interleaved execute() on one conn.
        store_skills = await asyncio.to_thread(self._read_store_state)
        health = await self._health_checker.check()

        # Build store_state list.
        store_entries = [
            SkillVersionEntry(
                skill_id=s.skill_id,
                canonical_name=s.canonical_name,
                version_id=s.active_version_id,
            )
            for s in store_skills
        ]

        # Build runtime_state list from cache.
        if self._cache is not None:
            cache_entries = [
                SkillVersionEntry(
                    skill_id=s.skill_id,
                    canonical_name=s.canonical_name,
                    version_id=s.active_version_id,
                )
                for s in self._cache.get_active_skills()
            ]
        else:
            cache_entries = []

        consistency = compute_consistency(store_entries, cache_entries)

        # Extract dep statuses from health response.
        assert health.dependencies is not None
        deps = health.dependencies
        store_ok = deps["runtime_store"].status == "ok"
        tel_ok = deps["telemetry_store"].status == "ok"
        embed_ok = deps["embedding_runtime"].status == "ok"
        cache_ok = deps["runtime_cache"].status == "ok"

        dep_readiness = DependencyReadiness(
            runtime_store=deps["runtime_store"].status,
            telemetry_store=deps["telemetry_store"].status,
            embedding_runtime=deps["embedding_runtime"].status,
            runtime_cache=deps["runtime_cache"].status,
            per_path=PathReadiness(
                compose=store_ok and embed_ok and cache_ok,
                retrieve=store_ok and embed_ok,
                inspect=store_ok,
                telemetry=tel_ok,
            ),
        )

        return RuntimeDiagnosticsResponse(
            cache_loaded=self._cache is not None,
            store_state=store_entries,
            runtime_state=cache_entries,
            consistency=consistency,
            dependency_readiness=dep_readiness,
        )

    def _read_store_state(self) -> list[ActiveSkill]:
        try:
            return get_active_skills(self._store)
        except Exception:
            return []


def compute_consistency(
    store_entries: list[SkillVersionEntry],
    cache_entries: list[SkillVersionEntry],
) -> ConsistencyReport:
    store_map = {e.skill_id: e.version_id for e in store_entries}
    cache_map = {e.skill_id: e.version_id for e in cache_entries}

    store_ids = set(store_map)
    cache_ids = set(cache_map)

    matched: list[str] = []
    version_mismatches: list[VersionMismatch] = []
    for sid in store_ids & cache_ids:
        if store_map[sid] == cache_map[sid]:
            matched.append(sid)
        else:
            version_mismatches.append(
                VersionMismatch(
                    skill_id=sid,
                    store_version_id=store_map[sid],
                    cache_version_id=cache_map[sid],
                )
            )

    missing_in_cache = sorted(store_ids - cache_ids)
    missing_in_store = sorted(cache_ids - store_ids)
    matched.sort()

    consistent = not missing_in_cache and not missing_in_store and not version_mismatches

    return ConsistencyReport(
        consistent=consistent,
        matched=matched,
        missing_in_cache=missing_in_cache,
        missing_in_store=missing_in_store,
        version_mismatches=version_mismatches,
    )


@router.get(
    "/diagnostics/runtime",
    response_model=RuntimeDiagnosticsResponse,
    summary="Runtime stale-content diagnostics: store vs cache consistency and dependency readiness",
)
async def runtime_diagnostics(request: Request) -> RuntimeDiagnosticsResponse:
    checker: DiagnosticsChecker | None = getattr(request.app.state, "diagnostics_checker", None)
    if checker is None:
        # No live checker (e.g. test without lifespan): return empty/consistent stub.
        return RuntimeDiagnosticsResponse(
            cache_loaded=False,
            store_state=[],
            runtime_state=[],
            consistency=ConsistencyReport(
                consistent=True,
                matched=[],
                missing_in_cache=[],
                missing_in_store=[],
                version_mismatches=[],
            ),
            dependency_readiness=DependencyReadiness(
                runtime_store="ok",
                telemetry_store="ok",
                embedding_runtime="ok",
                runtime_cache="ok",
                per_path=PathReadiness(
                    compose=True,
                    retrieve=True,
                    inspect=True,
                    telemetry=True,
                ),
            ),
        )
    return await checker.check()
