"""Active-version-only read queries against LadybugDB.

Non-active versions (``draft``, ``proposed``, ``superseded``) are invisible to
compose-time callers by construction: the underlying Cypher only traverses
``CURRENT_VERSION`` edges, and consistency guards raise
:class:`InconsistentActiveVersion` rather than silently fall through.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from agentalloy.reads.models import ActiveFragment, ActiveSkill, SkillClass

if TYPE_CHECKING:
    from agentalloy.storage.ladybug import LadybugStore  # pyright: ignore[reportUnusedImport]


class InconsistentActiveVersion(Exception):
    """Raised when CURRENT_VERSION state disagrees with the active-version contract."""

    def __init__(self, skill_id: str, reason: str) -> None:
        self.skill_id = skill_id
        self.reason = reason
        super().__init__(f"inconsistent active version for {skill_id}: {reason}")


# -------- public API --------


def get_active_skills(
    store: LadybugStore, *, skill_class: SkillClass | tuple[str, ...] | None = None
) -> list[ActiveSkill]:
    """Return every skill whose CURRENT_VERSION is active, after consistency checks."""
    _run_consistency_guard(store, skill_class=skill_class)

    params: dict[str, Any] = {}
    filters = "WHERE v.status = 'active' AND s.deprecated = false"
    if skill_class is not None:
        if isinstance(skill_class, tuple):
            params["skill_class"] = list(skill_class)
            filters += " AND s.skill_class IN $skill_class"
        else:
            params["skill_class"] = skill_class
            filters += " AND s.skill_class = $skill_class"

    cypher = f"""
    MATCH (s:Skill)-[:CURRENT_VERSION]->(v:SkillVersion)
    {filters}
    RETURN s.skill_id, s.canonical_name, s.category, s.skill_class,
           s.domain_tags, s.always_apply, s.phase_scope, s.category_scope,
           v.version_id, s.tier
    ORDER BY s.skill_id
    """
    return [_row_to_active_skill(row) for row in store.execute(cypher, params)]


def get_deprecated_skill_ids(store: LadybugStore) -> list[str]:
    """Return the skill_ids of every Skill node with ``deprecated = true``.

    Used by the DuckDB vector-store search methods to exclude deprecated
    skills' fragments from retrieval results, mirroring the ``deprecated = false``
    filter already applied to all Cypher-path active reads in this module.
    """
    cypher = "MATCH (s:Skill) WHERE s.deprecated = true RETURN s.skill_id"
    return [str(row[0]) for row in store.execute(cypher)]


def get_active_skill_by_id(store: LadybugStore, skill_id: str) -> ActiveSkill | None:
    """Single active skill lookup. Returns None if the skill is missing or has no active version."""
    # Targeted consistency check for just this skill
    _run_consistency_guard_for(store, skill_id)

    cypher = """
    MATCH (s:Skill {skill_id: $skill_id})-[:CURRENT_VERSION]->(v:SkillVersion)
    WHERE v.status = 'active' AND s.deprecated = false
    RETURN s.skill_id, s.canonical_name, s.category, s.skill_class,
           s.domain_tags, s.always_apply, s.phase_scope, s.category_scope,
           v.version_id, s.tier
    """
    rows = store.execute(cypher, {"skill_id": skill_id})
    if not rows:
        return None
    return _row_to_active_skill(rows[0])


def get_active_fragments(
    store: LadybugStore,
    *,
    skill_class: SkillClass | tuple[str, ...] | None = None,
    categories: list[str] | None = None,
    phases: list[str] | None = None,
    domain_tags: list[str] | None = None,
) -> list[ActiveFragment]:
    """Return fragments of active versions, optionally filtered by class, categories, tags.

    ``phases`` (authored phase_scope vocabulary) unions with ``categories``:
    a skill is eligible when either admits it. Passing ``phases`` alone
    filters on phase_scope only.
    """
    _run_consistency_guard(store, skill_class=skill_class)

    params: dict[str, Any] = {}
    filters = ["v.status = 'active'", "s.deprecated = false"]
    if skill_class is not None:
        if isinstance(skill_class, tuple):
            params["skill_class"] = list(skill_class)
            filters.append("s.skill_class IN $skill_class")
        else:
            params["skill_class"] = skill_class
            filters.append("s.skill_class = $skill_class")
    if categories is not None and phases:
        params["categories"] = list(categories)
        params["phases"] = list(phases)
        filters.append(
            "(s.category IN $categories"
            " OR (s.phase_scope IS NOT NULL"
            " AND ANY(p IN $phases WHERE p IN s.phase_scope)))"
        )
    elif categories is not None:
        params["categories"] = list(categories)
        filters.append("s.category IN $categories")
    elif phases:
        params["phases"] = list(phases)
        filters.append("(s.phase_scope IS NOT NULL AND ANY(p IN $phases WHERE p IN s.phase_scope))")
    if domain_tags is not None:
        params["domain_tags"] = list(domain_tags)
        # ANY of the requested tags must be present on the skill.
        filters.append("ANY(t IN $domain_tags WHERE t IN s.domain_tags)")

    where_clause = "WHERE " + " AND ".join(filters)
    cypher = f"""
    MATCH (s:Skill)-[:CURRENT_VERSION]->(v:SkillVersion)-[:DECOMPOSES_TO]->(f:Fragment)
    {where_clause}
    RETURN f.fragment_id, f.fragment_type, f.sequence, f.content,
           s.skill_id, v.version_id, s.skill_class, s.category, s.domain_tags,
           s.phase_scope
    ORDER BY s.skill_id, f.sequence
    """
    return [_row_to_active_fragment(row) for row in store.execute(cypher, params)]


def get_active_fragments_for_skill(store: LadybugStore, skill_id: str) -> list[ActiveFragment]:
    """Fragments of the active version of a single skill."""
    _run_consistency_guard_for(store, skill_id)

    cypher = """
    MATCH (s:Skill {skill_id: $skill_id})-[:CURRENT_VERSION]->(v:SkillVersion)
        -[:DECOMPOSES_TO]->(f:Fragment)
    WHERE v.status = 'active' AND s.deprecated = false
    RETURN f.fragment_id, f.fragment_type, f.sequence, f.content,
           s.skill_id, v.version_id, s.skill_class, s.category, s.domain_tags,
           s.phase_scope
    ORDER BY f.sequence
    """
    return [_row_to_active_fragment(row) for row in store.execute(cypher, {"skill_id": skill_id})]


def get_active_version_by_id(store: LadybugStore, version_id: str) -> dict[str, Any]:
    """Return raw SkillVersion data, enforcing that the version is active.

    Raises :class:`InconsistentActiveVersion` if the version exists but is not
    active.  Raises :class:`RuntimeError` if the version is not found at all.
    This is the single enforced gate for version-id-based fetches; callers must
    not query SkillVersion rows directly without going through this function.
    """
    rows = store.execute(
        """
        MATCH (v:SkillVersion {version_id: $vid})
        RETURN v.version_id, v.version_number, v.authored_at, v.author,
               v.change_summary, v.raw_prose, v.status
        """,
        {"vid": version_id},
    )
    if not rows:
        raise RuntimeError(f"version {version_id!r} not found")
    row = rows[0]
    status = str(row[6])
    if status != "active":
        # Derive a best-effort skill_id from the version_id or report unknown.
        skill_rows = store.execute(
            "MATCH (s:Skill)-[:HAS_VERSION]->(v:SkillVersion {version_id: $vid}) RETURN s.skill_id",
            {"vid": version_id},
        )
        skill_id = str(skill_rows[0][0]) if skill_rows else f"<unknown skill for {version_id}>"
        raise InconsistentActiveVersion(
            skill_id,
            f"version {version_id!r} has status={status!r}, expected 'active'",
        )
    return {
        "version_id": str(row[0]),
        "version_number": int(row[1]),
        "authored_at": row[2],
        "author": str(row[3]),
        "change_summary": str(row[4]),
        "raw_prose": str(row[5]),
    }


# -------- consistency --------


def _run_consistency_guard(
    store: LadybugStore, *, skill_class: SkillClass | tuple[str, ...] | None = None
) -> None:
    """Scan for CURRENT_VERSION / active-version mismatches. Raises on first inconsistency.

    For solo-scale corpora (tens of skills) this is cheap. If the corpus grows, move
    this to a startup-time check and a scheduled audit.
    """
    params: dict[str, Any] = {}
    if skill_class is None:
        class_filter = ""
    elif isinstance(skill_class, tuple):
        params["skill_class"] = list(skill_class)
        class_filter = " WHERE s.skill_class IN $skill_class"
    else:
        params["skill_class"] = skill_class
        class_filter = " WHERE s.skill_class = $skill_class"

    # (a) CURRENT_VERSION points at non-active version.
    cypher_a = f"""
    MATCH (s:Skill)-[:CURRENT_VERSION]->(v:SkillVersion)
    {class_filter}
    WITH s, v
    WHERE v.status <> 'active'
    RETURN s.skill_id, v.status
    LIMIT 1
    """
    rows = store.execute(cypher_a, params)
    if rows:
        sid, status = rows[0][0], rows[0][1]
        raise InconsistentActiveVersion(sid, f"CURRENT_VERSION points at status={status!r} version")

    # (b) No CURRENT_VERSION but the skill has an active version in HAS_VERSION.
    cypher_b = f"""
    MATCH (s:Skill)-[:HAS_VERSION]->(av:SkillVersion {{status: 'active'}})
    {class_filter}
    WITH s, av
    WHERE NOT EXISTS {{ MATCH (s)-[:CURRENT_VERSION]->(:SkillVersion) }}
    RETURN s.skill_id
    LIMIT 1
    """
    rows = store.execute(cypher_b, params)
    if rows:
        sid = rows[0][0]
        raise InconsistentActiveVersion(
            sid, "active SkillVersion exists but no CURRENT_VERSION edge"
        )


def _run_consistency_guard_for(store: LadybugStore, skill_id: str) -> None:
    # Scoped version of _run_consistency_guard for a single skill_id.
    cypher_a = """
    MATCH (s:Skill {skill_id: $skill_id})-[:CURRENT_VERSION]->(v:SkillVersion)
    WHERE v.status <> 'active'
    RETURN v.status
    LIMIT 1
    """
    rows = store.execute(cypher_a, {"skill_id": skill_id})
    if rows:
        raise InconsistentActiveVersion(
            skill_id, f"CURRENT_VERSION points at status={rows[0][0]!r} version"
        )

    cypher_b = """
    MATCH (s:Skill {skill_id: $skill_id})-[:HAS_VERSION]->(av:SkillVersion {status: 'active'})
    WHERE NOT EXISTS { MATCH (s)-[:CURRENT_VERSION]->(:SkillVersion) }
    RETURN s.skill_id
    LIMIT 1
    """
    rows = store.execute(cypher_b, {"skill_id": skill_id})
    if rows:
        raise InconsistentActiveVersion(
            skill_id, "active SkillVersion exists but no CURRENT_VERSION edge"
        )


# -------- row mapping --------


def _row_to_active_skill(row: list[Any]) -> ActiveSkill:
    return ActiveSkill(
        skill_id=cast("str", row[0]),
        canonical_name=cast("str", row[1]),
        category=cast("str", row[2]),
        skill_class=cast("SkillClass", row[3]),
        domain_tags=list(cast("list[str]", row[4])),
        always_apply=bool(row[5]),
        phase_scope=_optional_list(row[6]),
        category_scope=_optional_list(row[7]),
        active_version_id=cast("str", row[8]),
        tier=cast("str | None", row[9]) if len(row) > 9 else None,
    )


def _row_to_active_fragment(row: list[Any]) -> ActiveFragment:
    raw_scope = row[9] if len(row) > 9 else None
    return ActiveFragment(
        fragment_id=cast("str", row[0]),
        fragment_type=cast("str", row[1]),
        sequence=int(cast("int", row[2])),
        content=cast("str", row[3]),
        skill_id=cast("str", row[4]),
        version_id=cast("str", row[5]),
        skill_class=cast("SkillClass", row[6]),
        category=cast("str", row[7]),
        domain_tags=list(cast("list[str]", row[8])),
        phase_scope=tuple(cast("list[str]", raw_scope)) if raw_scope else None,
    )


def _optional_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, list) and not value:
        # Kuzu stores `null` list columns as empty lists; treat empty as None
        # so downstream "no scope" checks are uniform.
        return None
    return list(cast("list[str]", value))
