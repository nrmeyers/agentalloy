"""Active-version-only read queries against the DuckDB skill store.

Ported from Cypher to SQL (``agentalloy.duck``) in the v5 two-engine
rebuild. The graph edges are folded into relational columns/tables:
``CURRENT_VERSION`` -> ``skills.current_version_id``; ``HAS_VERSION`` ->
``skill_versions.skill_id``; ``DECOMPOSES_TO`` -> ``fragments.version_id``.

Non-active versions remain invisible to compose-time callers by construction:
queries only join on ``current_version_id`` where ``status = 'active'``, and the
consistency guards raise :class:`InconsistentActiveVersionError` rather than silently
fall through. Behaviour (row order, null-list normalization, guard semantics) is
preserved 1:1 with the v5.3 Cypher path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agentalloy.reads.models import ActiveFragment, ActiveSkill, SkillClass
from agentalloy.storage.protocols import FragmentRow, SkillRow

if TYPE_CHECKING:
    from agentalloy.storage.protocols import SkillStore  # pyright: ignore[reportUnusedImport]


class InconsistentActiveVersionError(Exception):
    """Raised when CURRENT_VERSION state disagrees with the active-version contract."""

    def __init__(self, skill_id: str, reason: str) -> None:
        self.skill_id = skill_id
        self.reason = reason
        super().__init__(f"inconsistent active version for {skill_id}: {reason}")


# -------- DTO mapping --------


def _skill_row_to_active(row: SkillRow) -> ActiveSkill:
    """Map a store ``SkillRow`` to the read-path ``ActiveSkill`` DTO."""
    return ActiveSkill(
        skill_id=row.skill_id,
        canonical_name=row.canonical_name,
        category=row.category,
        skill_class=row.skill_class,  # type: ignore[arg-type]
        domain_tags=list(row.domain_tags),
        always_apply=row.always_apply,
        phase_scope=list(row.phase_scope) if row.phase_scope else None,
        category_scope=list(row.category_scope) if row.category_scope else None,
        active_version_id=row.current_version_id,
        tier=row.tier,
        description=row.description,
    )


def _fragment_row_to_active(
    frag: FragmentRow,
    skill: SkillRow,
) -> ActiveFragment:
    """Map a store ``FragmentRow`` + parent ``SkillRow`` to ``ActiveFragment``."""
    return ActiveFragment(
        fragment_id=frag.fragment_id,
        fragment_type=frag.fragment_type,
        sequence=frag.sequence,
        content=frag.content,
        skill_id=skill.skill_id,
        version_id=frag.version_id,
        skill_class=skill.skill_class,  # type: ignore[arg-type]
        category=skill.category,
        domain_tags=list(skill.domain_tags),
        phase_scope=tuple(skill.phase_scope) if skill.phase_scope else None,
        description=skill.description,
        category_scope=tuple(skill.category_scope) if skill.category_scope else None,
    )


# -------- public API --------


def get_active_skills(
    store: SkillStore,
    *,
    skill_class: SkillClass | tuple[str, ...] | None = None,
) -> list[ActiveSkill]:
    """Return every skill whose CURRENT_VERSION is active, after consistency checks."""
    _run_consistency_guard(store, skill_class=skill_class)
    rows = store.get_active_skills(skill_class=skill_class)
    return [_skill_row_to_active(r) for r in rows]


def get_deprecated_skill_ids(store: SkillStore) -> list[str]:
    """Return the skill_ids of every skill with ``deprecated = true``."""
    return store.get_deprecated_skill_ids()


def get_active_skill_by_id(store: SkillStore, skill_id: str) -> ActiveSkill | None:
    """Single active skill lookup. None if missing or no active version."""
    _run_consistency_guard_for(store, skill_id)
    row = store.get_active_skill_by_id(skill_id)
    if row is None:
        return None
    return _skill_row_to_active(row)


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

    # Fetch all active skills (unfiltered by fragment-level predicates) so we
    # can build a version_id → SkillRow lookup. The store's get_active_fragments
    # returns bare FragmentRow objects; we re-attach the parent skill metadata
    # that ActiveFragment carries.
    all_skills = store.get_active_skills()
    version_to_skill: dict[str, SkillRow] = {
        s.current_version_id: s for s in all_skills
    }

    fragments = store.get_active_fragments(
        skill_class=skill_class,
        categories=categories,
        phases=phases,
        domain_tags=domain_tags,
    )

    result: list[ActiveFragment] = []
    for frag in fragments:
        skill = version_to_skill.get(frag.version_id)
        if skill is None:
            continue  # fragment's parent skill is not active (shouldn't happen after guard)
        result.append(_fragment_row_to_active(frag, skill))
    return result


def get_active_fragments_for_skill(store: SkillStore, skill_id: str) -> list[ActiveFragment]:
    """Fragments of the active version of a single skill."""
    _run_consistency_guard_for(store, skill_id)

    skill = store.get_active_skill_by_id(skill_id)
    if skill is None:
        return []
    fragments = store.get_active_fragments_for_skill(skill_id)
    return [_fragment_row_to_active(frag, skill) for frag in fragments]


def get_active_version_by_id(store: SkillStore, version_id: str) -> dict[str, Any]:
    """Return raw SkillVersion data, enforcing that the version is active.

    Raises :class:`InconsistentActiveVersionError` if the version exists but is not
    active; :class:`RuntimeError` if not found at all. The single enforced gate
    for version-id-based fetches.
    """
    version = store.get_version(version_id)
    if version is None:
        raise RuntimeError(f"version {version_id!r} not found")
    if version.status != "active":
        raise InconsistentActiveVersionError(
            version.skill_id,
            f"version {version_id!r} has status={version.status!r}, expected 'active'",
        )
    return {
        "version_id": version.version_id,
        "version_number": version.version_number,
        "authored_at": version.authored_at,
        "author": version.author,
        "change_summary": version.change_summary,
        "raw_prose": version.raw_prose,
    }


# -------- consistency --------


def _run_consistency_guard(
    store: SkillStore,
    *,
    skill_class: SkillClass | tuple[str, ...] | None = None,
) -> None:
    """Scan for CURRENT_VERSION / active-version mismatches. Raises on first one."""
    store.check_consistency(skill_class=skill_class)


def _run_consistency_guard_for(store: SkillStore, skill_id: str) -> None:
    """Scoped single-skill variant of :func:`_run_consistency_guard`."""
    store.check_consistency_for(skill_id)


# -------- row mapping helpers (kept for test imports) --------


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
    return list(value)
