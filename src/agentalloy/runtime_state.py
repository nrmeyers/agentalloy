"""Startup-time active-skill cache (NXS-777).

Loaded once during lifespan from the runtime store. Compose and retrieve
handlers read exclusively from this snapshot — no per-request DB hits on
the hot path.  A restart is required to pick up re-seeded data.

If loading fails the cache remains ``None``; the health endpoint reflects
``unavailable`` and runtime handlers 503 rather than returning stale or
partial data.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from agentalloy.api.compose_models import Phase
from agentalloy.reads.active import (
    get_active_fragments,
    get_active_skills,
)
from agentalloy.reads.models import ActiveFragment, ActiveSkill, SkillClass
from agentalloy.retrieval.domain import phase_to_categories
from agentalloy.storage.ladybug import LadybugStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VersionDetail:
    """Raw prose + metadata for a single SkillVersion — cached at startup."""

    version_id: str
    version_number: int
    authored_at: Any  # datetime — kept as-is from Kuzu
    author: str
    change_summary: str
    raw_prose: str


class RuntimeCache:
    """Immutable snapshot of all active skill data, loaded at startup.

    Public surface intentionally mirrors the store-backed read functions so
    callers (orchestrators) require minimal changes.
    """

    def __init__(
        self,
        skills: dict[str, ActiveSkill],
        fragments: list[ActiveFragment],
        version_details: dict[str, VersionDetail],
        deprecated_skill_ids: list[str] | None = None,
        requires_edges: dict[str, list[str]] | None = None,
    ) -> None:
        self._skills: dict[str, ActiveSkill] = skills
        self._fragments: list[ActiveFragment] = fragments
        self._version_details: dict[str, VersionDetail] = version_details
        self._deprecated_skill_ids: list[str] = list(deprecated_skill_ids or [])
        # REQUIRES_COMPOSITIONAL out-edges: {skill_id: [required_skill_id, ...]}.
        # Loaded at startup so graph expansion (RETRIEVAL_GRAPH_EXPAND) needs no
        # per-request DB hop. Empty on a corpus with no declared edges.
        self._requires_edges: dict[str, list[str]] = dict(requires_edges or {})
        self.skill_count: int = len(skills)
        self.fragment_count: int = len(fragments)

    # ---- skill reads ----

    def get_active_skill_by_id(self, skill_id: str) -> ActiveSkill | None:
        return self._skills.get(skill_id)

    def get_active_skills(
        self, *, skill_class: SkillClass | tuple[str, ...] | None = None
    ) -> list[ActiveSkill]:
        skills = list(self._skills.values())
        if skill_class is not None:
            if isinstance(skill_class, tuple):
                skills = [s for s in skills if s.skill_class in skill_class]
            else:
                skills = [s for s in skills if s.skill_class == skill_class]
        return sorted(skills, key=lambda s: s.skill_id)

    # ---- fragment reads ----

    def get_active_fragments(
        self,
        *,
        skill_class: SkillClass | tuple[str, ...] | None = None,
        categories: list[str] | None = None,
        phases: list[str] | None = None,
        domain_tags: list[str] | None = None,
    ) -> list[ActiveFragment]:
        result: list[ActiveFragment] = list(self._fragments)
        if skill_class is not None:
            if isinstance(skill_class, tuple):
                result = [f for f in result if f.skill_class in skill_class]
            else:
                result = [f for f in result if f.skill_class == skill_class]
        if categories is not None and phases:
            # Union eligibility: category map OR authored phase_scope.
            cat_set = set(categories)
            phase_set = set(phases)
            result = [
                f
                for f in result
                if f.category in cat_set
                or (f.phase_scope and any(p in phase_set for p in f.phase_scope))
            ]
        elif categories is not None:
            cat_set = set(categories)
            result = [f for f in result if f.category in cat_set]
        elif phases:
            phase_set = set(phases)
            result = [
                f for f in result if f.phase_scope and any(p in phase_set for p in f.phase_scope)
            ]
        if domain_tags is not None:
            tag_set = set(domain_tags)
            result = [f for f in result if any(t in tag_set for t in f.domain_tags)]
        return result

    def get_required_skill_ids(self, skill_id: str) -> list[str]:
        """REQUIRES_COMPOSITIONAL targets of ``skill_id`` (one hop, requires only).

        Used by graph-expansion retrieval (RETRIEVAL_GRAPH_EXPAND). Returns []
        for unknown skills or a corpus with no declared edges."""
        return list(self._requires_edges.get(skill_id, []))

    def get_deprecated_skill_ids(self) -> list[str]:
        """Return skill_ids of all skills flagged ``deprecated = true``.

        The cache itself loads only non-deprecated skills (via the active reads
        which filter ``deprecated = false``); this list is captured separately
        at load time so retrieval can exclude deprecated fragments from the
        DuckDB vector store, which is populated independently of the cache.
        """
        return list(self._deprecated_skill_ids)

    def get_active_fragments_for_skill(self, skill_id: str) -> list[ActiveFragment]:
        return sorted(
            [f for f in self._fragments if f.skill_id == skill_id],
            key=lambda f: f.sequence,
        )

    def get_active_fragments_for_phase(
        self,
        phase: Phase,
        *,
        domain_tags: list[str] | None = None,
    ) -> list[ActiveFragment]:
        """Convenience: fragments eligible for a compose/retrieve phase."""
        from agentalloy.retrieval.domain import phase_to_scope_terms

        return self.get_active_fragments(
            skill_class=("domain", "workflow"),
            categories=phase_to_categories(phase),
            phases=phase_to_scope_terms(phase) or None,
            domain_tags=domain_tags,
        )

    # ---- version detail reads ----

    def get_version_detail(self, version_id: str) -> VersionDetail | None:
        return self._version_details.get(version_id)


_PROFILE_CACHE_LOCK = threading.Lock()
_PROFILE_CACHE_MAX = 4
# OrderedDict used as LRU: newest at end, evict from front.
_PROFILE_CACHES: OrderedDict[str, RuntimeCache] = OrderedDict()


def get_profile_cache(profile_name: str) -> RuntimeCache | None:
    """Return the cached RuntimeCache for a profile, or None if not loaded."""
    with _PROFILE_CACHE_LOCK:
        cache = _PROFILE_CACHES.get(profile_name)
        if cache is not None:
            # Move to end (most recently used)
            _PROFILE_CACHES.move_to_end(profile_name)
        return cache


def set_profile_cache(profile_name: str, cache: RuntimeCache) -> None:
    """Store a RuntimeCache for a profile, evicting LRU if needed."""
    with _PROFILE_CACHE_LOCK:
        _PROFILE_CACHES[profile_name] = cache
        _PROFILE_CACHES.move_to_end(profile_name)
        while len(_PROFILE_CACHES) > _PROFILE_CACHE_MAX:
            _PROFILE_CACHES.popitem(last=False)


def invalidate_profile_cache(profile_name: str) -> None:
    """Remove the cached RuntimeCache for a profile."""
    with _PROFILE_CACHE_LOCK:
        _PROFILE_CACHES.pop(profile_name, None)


def load_runtime_cache(store: LadybugStore) -> RuntimeCache:
    """Query the store and build a ``RuntimeCache``.

    Raises on any store or consistency error — the caller (lifespan) should
    catch and record the failure rather than propagating through FastAPI's
    startup.
    """
    logger.info("Loading active skill data into runtime cache …")

    skills_list = get_active_skills(store)
    skills_by_id: dict[str, ActiveSkill] = {s.skill_id: s for s in skills_list}

    fragments = get_active_fragments(store)

    # Collect all unique version_ids we need details for.
    version_ids: set[str] = {s.active_version_id for s in skills_list}

    version_details: dict[str, VersionDetail] = {}
    for vid in version_ids:
        rows = store.execute(
            """
            MATCH (v:SkillVersion {version_id: $vid})
            RETURN v.version_id, v.version_number, v.authored_at, v.author,
                   v.change_summary, v.raw_prose
            """,
            {"vid": vid},
        )
        if not rows:
            raise RuntimeError(f"version {vid!r} not found during cache load")
        row = rows[0]
        version_details[vid] = VersionDetail(
            version_id=str(row[0]),
            version_number=int(row[1]),
            authored_at=row[2],
            author=str(row[3]),
            change_summary=str(row[4]),
            raw_prose=str(row[5]),
        )

    from agentalloy.reads import get_deprecated_skill_ids as _get_deprecated_ids

    deprecated_ids = _get_deprecated_ids(store)

    # REQUIRES_COMPOSITIONAL edges for graph-expansion retrieval. Empty on a
    # corpus that declares none. One query; grouped into {source: [target,...]}.
    requires_edges: dict[str, list[str]] = {}
    edge_rows = store.execute(
        "MATCH (s:Skill)-[:REQUIRES_COMPOSITIONAL]->(t:Skill) RETURN s.skill_id, t.skill_id"
    )
    for row in edge_rows:
        requires_edges.setdefault(str(row[0]), []).append(str(row[1]))

    cache = RuntimeCache(
        skills=skills_by_id,
        fragments=fragments,
        version_details=version_details,
        deprecated_skill_ids=deprecated_ids,
        requires_edges=requires_edges,
    )
    logger.info(
        "Runtime cache loaded: %d skills, %d fragments, %d deprecated",
        cache.skill_count,
        cache.fragment_count,
        len(deprecated_ids),
    )
    return cache


def load_profile_runtime_cache(profile_name: str) -> RuntimeCache:
    """Load a RuntimeCache from a profile's DuckDB (profile_skills table).

    Falls back to an empty cache if the profile has no datastore or no
    profile_skills table yet (soft-fail — the domain cache handles retrieval).
    """
    # Check in-memory LRU first
    cached = get_profile_cache(profile_name)
    if cached is not None:
        return cached

    try:
        import duckdb

        from agentalloy.profiles import profile_datastore_path

        ds_path = profile_datastore_path(profile_name)
        if not ds_path.exists():
            return _empty_runtime_cache()

        conn = duckdb.connect(str(ds_path), read_only=True)
        try:
            rows = conn.execute(
                "SELECT skill_id, skill_class, canonical_name, raw_prose FROM profile_skills"
            ).fetchall()
        except duckdb.CatalogException:
            # Table doesn't exist yet
            return _empty_runtime_cache()
        finally:
            conn.close()
    except Exception:
        logger.debug("profile cache load failed for %s, returning empty", profile_name)
        return _empty_runtime_cache()

    skills_by_id: dict[str, ActiveSkill] = {}
    for row in rows:
        skill_id, skill_class, canonical_name, _raw_prose = row
        # ActiveSkill requires active_version_id — use skill_id as a stand-in.
        skills_by_id[str(skill_id)] = ActiveSkill(
            skill_id=str(skill_id),
            canonical_name=str(canonical_name),
            category="",
            skill_class=str(skill_class),  # type: ignore[arg-type]
            domain_tags=[],
            always_apply=False,
            phase_scope=None,
            category_scope=None,
            active_version_id=str(skill_id),
            tier=None,
        )

    cache = RuntimeCache(
        skills=skills_by_id,
        fragments=[],
        version_details={},
    )
    set_profile_cache(profile_name, cache)
    logger.info("Profile cache loaded: %d skills for '%s'", len(skills_by_id), profile_name)
    return cache


def _empty_runtime_cache() -> RuntimeCache:
    return RuntimeCache(skills={}, fragments=[], version_details={})
