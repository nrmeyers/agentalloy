"""Active-version-only read queries against the DuckDB skill store.

Ported from Cypher to SQL (``agentalloy.duck``) in the v5 two-engine
rebuild. The graph edges are folded into relational columns/tables:
``CURRENT_VERSION`` -> ``skills.current_version_id``; ``HAS_VERSION`` ->
``skill_versions.skill_id``; ``DECOMPOSES_TO`` -> ``fragments.version_id``.

Non-active versions remain invisible to compose-time callers by construction:
queries only join on ``current_version_id`` where ``status = 'active'``, and the
consistency guards raise :class:`InconsistentActiveVersion` rather than silently
fall through. Behaviour (row order, null-list normalization, guard semantics) is
preserved 1:1 with the v5.3 Cypher path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from agentalloy.reads.models import ActiveFragment, ActiveSkill, SkillClass

if TYPE_CHECKING:
    from agentalloy.storage.protocols import SkillStore  # pyright: ignore[reportUnusedImport]


class InconsistentActiveVersion(Exception):
    """Raised when CURRENT_VERSION state disagrees with the active-version contract."""

    def __init__(self, skill_id: str, reason: str) -> None:
        self.skill_id = skill_id
        self.reason = reason
        super().__init__(f"inconsistent active version for {skill_id}: {reason}")


# Column projections that feed the row-mappers below (order is load-bearing).
_SKILL_COLS = (
    "s.skill_id, s.canonical_name, s.category, s.skill_class, "
    "s.domain_tags, s.always_apply, s.phase_scope, s.category_scope, "
    "v.version_id, s.tier, s.description"
)
_FRAGMENT_COLS = (
    "f.fragment_id, f.fragment_type, f.sequence, f.content, "
    "s.skill_id, v.version_id, s.skill_class, s.category, s.domain_tags, "
    "s.phase_scope, s.description"
)
_ACTIVE_JOIN = "FROM skills s JOIN skill_versions v ON v.version_id = s.current_version_id"
_FRAGMENT_JOIN = _ACTIVE_JOIN + " JOIN fragments f ON f.version_id = v.version_id"


def _class_pred(
    skill_class: SkillClass | tuple[str, ...] | None, params: dict[str, Any]
) -> str | None:
    """Build a skill_class predicate, recording the param. None when unfiltered."""
    if skill_class is None:
        return None
    if isinstance(skill_class, tuple):
        params["skill_class"] = list(skill_class)
        return "list_contains($skill_class, s.skill_class)"
    params["skill_class"] = skill_class
    return "s.skill_class = $skill_class"


# -------- public API --------


def get_active_skills(
    store: SkillStore, *, skill_class: SkillClass | tuple[str, ...] | None = None
) -> list[ActiveSkill]:
    """Return every skill whose CURRENT_VERSION is active, after consistency checks."""
    _run_consistency_guard(store, skill_class=skill_class)

    params: dict[str, Any] = {}
    filters = ["v.status = 'active'", "s.deprecated = false"]
    cls = _class_pred(skill_class, params)
    if cls:
        filters.append(cls)

    sql = f"SELECT {_SKILL_COLS} {_ACTIVE_JOIN} WHERE {' AND '.join(filters)} ORDER BY s.skill_id"
    return [_row_to_active_skill(row) for row in store.execute(sql, params)]


def get_deprecated_skill_ids(store: SkillStore) -> list[str]:
    """Return the skill_ids of every skill with ``deprecated = true``."""
    sql = "SELECT skill_id FROM skills WHERE deprecated = true"
    return [str(row[0]) for row in store.execute(sql)]


def get_active_skill_by_id(store: SkillStore, skill_id: str) -> ActiveSkill | None:
    """Single active skill lookup. None if missing or no active version."""
    _run_consistency_guard_for(store, skill_id)

    sql = (
        f"SELECT {_SKILL_COLS} {_ACTIVE_JOIN} "
        "WHERE s.skill_id = $skill_id AND v.status = 'active' AND s.deprecated = false"
    )
    rows = store.execute(sql, {"skill_id": skill_id})
    if not rows:
        return None
    return _row_to_active_skill(rows[0])


def get_active_fragments(
    store: SkillStore,
    *,
    skill_class: SkillClass | tuple[str, ...] | None = None,
    categories: list[str] | None = None,
    phases: list[str] | None = None,
    domain_tags: list[str] | None = None,
) -> list[ActiveFragment]:
    """Fragments of active versions, optionally filtered by class/categories/phases/tags.

    ``phases`` (authored phase_scope) unions with ``categories``: either admits a
    skill. Passing ``phases`` alone filters on phase_scope only.
    """
    _run_consistency_guard(store, skill_class=skill_class)

    params: dict[str, Any] = {}
    filters = ["v.status = 'active'", "s.deprecated = false"]
    cls = _class_pred(skill_class, params)
    if cls:
        filters.append(cls)
    if categories is not None and phases:
        params["categories"] = list(categories)
        params["phases"] = list(phases)
        filters.append(
            "(list_contains($categories, s.category)"
            " OR (s.phase_scope IS NOT NULL AND list_has_any(s.phase_scope, $phases)))"
        )
    elif categories is not None:
        params["categories"] = list(categories)
        filters.append("list_contains($categories, s.category)")
    elif phases:
        params["phases"] = list(phases)
        filters.append("(s.phase_scope IS NOT NULL AND list_has_any(s.phase_scope, $phases))")
    if domain_tags is not None:
        params["domain_tags"] = list(domain_tags)
        filters.append("(s.domain_tags IS NOT NULL AND list_has_any(s.domain_tags, $domain_tags))")

    sql = (
        f"SELECT {_FRAGMENT_COLS} {_FRAGMENT_JOIN} "
        f"WHERE {' AND '.join(filters)} ORDER BY s.skill_id, f.sequence"
    )
    return [_row_to_active_fragment(row) for row in store.execute(sql, params)]


def get_active_fragments_for_skill(store: SkillStore, skill_id: str) -> list[ActiveFragment]:
    """Fragments of the active version of a single skill."""
    _run_consistency_guard_for(store, skill_id)

    sql = (
        f"SELECT {_FRAGMENT_COLS} {_FRAGMENT_JOIN} "
        "WHERE s.skill_id = $skill_id AND v.status = 'active' AND s.deprecated = false "
        "ORDER BY f.sequence"
    )
    return [_row_to_active_fragment(row) for row in store.execute(sql, {"skill_id": skill_id})]


def get_active_version_by_id(store: SkillStore, version_id: str) -> dict[str, Any]:
    """Return raw SkillVersion data, enforcing that the version is active.

    Raises :class:`InconsistentActiveVersion` if the version exists but is not
    active; :class:`RuntimeError` if not found at all. The single enforced gate
    for version-id-based fetches.
    """
    rows = store.execute(
        "SELECT version_id, version_number, authored_at, author, "
        "change_summary, raw_prose, status FROM skill_versions WHERE version_id = $vid",
        {"vid": version_id},
    )
    if not rows:
        raise RuntimeError(f"version {version_id!r} not found")
    row = rows[0]
    status = str(row[6])
    if status != "active":
        skill_rows = store.execute(
            "SELECT skill_id FROM skill_versions WHERE version_id = $vid", {"vid": version_id}
        )
        skill_id = str(skill_rows[0][0]) if skill_rows else f"<unknown skill for {version_id}>"
        raise InconsistentActiveVersion(
            skill_id, f"version {version_id!r} has status={status!r}, expected 'active'"
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
    store: SkillStore, *, skill_class: SkillClass | tuple[str, ...] | None = None
) -> None:
    """Scan for CURRENT_VERSION / active-version mismatches. Raises on first one."""
    params: dict[str, Any] = {}
    cls = _class_pred(skill_class, params)
    class_and = f" AND {cls}" if cls else ""

    # (a) CURRENT_VERSION points at a non-active version.
    rows = store.execute(
        f"SELECT s.skill_id, v.status {_ACTIVE_JOIN} WHERE v.status <> 'active'{class_and} LIMIT 1",
        params,
    )
    if rows:
        sid, status = rows[0][0], rows[0][1]
        raise InconsistentActiveVersion(sid, f"CURRENT_VERSION points at status={status!r} version")

    # (b) An active version exists (HAS_VERSION) but there is no CURRENT_VERSION edge.
    rows = store.execute(
        "SELECT s.skill_id FROM skills s "
        "JOIN skill_versions av ON av.skill_id = s.skill_id AND av.status = 'active' "
        "LEFT JOIN skill_versions cur ON cur.version_id = s.current_version_id "
        f"WHERE cur.version_id IS NULL{class_and} LIMIT 1",
        params,
    )
    if rows:
        raise InconsistentActiveVersion(
            rows[0][0], "active SkillVersion exists but no CURRENT_VERSION edge"
        )


def _run_consistency_guard_for(store: SkillStore, skill_id: str) -> None:
    """Scoped single-skill variant of :func:`_run_consistency_guard`."""
    rows = store.execute(
        f"SELECT v.status {_ACTIVE_JOIN} "
        "WHERE s.skill_id = $skill_id AND v.status <> 'active' LIMIT 1",
        {"skill_id": skill_id},
    )
    if rows:
        raise InconsistentActiveVersion(
            skill_id, f"CURRENT_VERSION points at status={rows[0][0]!r} version"
        )

    rows = store.execute(
        "SELECT s.skill_id FROM skills s "
        "JOIN skill_versions av ON av.skill_id = s.skill_id AND av.status = 'active' "
        "LEFT JOIN skill_versions cur ON cur.version_id = s.current_version_id "
        "WHERE s.skill_id = $skill_id AND cur.version_id IS NULL LIMIT 1",
        {"skill_id": skill_id},
    )
    if rows:
        raise InconsistentActiveVersion(
            skill_id, "active SkillVersion exists but no CURRENT_VERSION edge"
        )


# -------- row mapping --------


def _row_to_active_skill(row: Any) -> ActiveSkill:
    return ActiveSkill(
        skill_id=cast("str", row[0]),
        canonical_name=cast("str", row[1]),
        category=cast("str", row[2]),
        skill_class=cast("SkillClass", row[3]),
        domain_tags=list(row[4] or []),
        always_apply=bool(row[5]),
        phase_scope=_optional_list(row[6]),
        category_scope=_optional_list(row[7]),
        active_version_id=cast("str", row[8]),
        tier=cast("str | None", row[9]) if len(row) > 9 else None,
        description=_optional_str(row[10]) if len(row) > 10 else None,
    )


def _row_to_active_fragment(row: Any) -> ActiveFragment:
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
        domain_tags=list(row[8] or []),
        phase_scope=tuple(cast("list[str]", raw_scope)) if raw_scope else None,
        description=_optional_str(row[10]) if len(row) > 10 else None,
    )


def _optional_str(value: Any) -> str | None:
    """Normalize a TEXT column to ``str | None`` (NULL or blank -> None)."""
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _optional_list(value: Any) -> list[str] | None:
    """Normalize a TEXT[] column to ``list[str] | None`` (NULL or empty -> None)."""
    if value is None:
        return None
    if isinstance(value, list) and not value:
        return None
    return list(cast("list[str]", value))
