"""System-skill applicability evaluation (NXS-770).

Pure predicate logic — no LLM, no DB calls. Evaluates phase_scope,
category_scope, and always_apply against a compose request's context to
determine which active system skills participate in a given composition.
"""

from __future__ import annotations

from agentalloy.reads.models import ActiveSkill


def filter_applicable_system_skills(
    skills: list[ActiveSkill],
    *,
    phase: str | None,
    category: str | None,
) -> list[ActiveSkill]:
    """Return every system skill from *skills* whose applicability predicate matches.

    Rules (order matters; first match wins inclusion):
    1. ``always_apply=True`` — include regardless of phase or category.
    2. ``phase_scope`` is set and *phase* is in it — check category next:
       - ``category_scope`` is ``None`` — no category restriction, include.
       - ``category_scope`` is set and *category* is in it — include.
       - ``category_scope`` is set but *category* is not in it — exclude.
    3. ``phase_scope`` is ``None`` and ``always_apply`` is ``False`` — exclude.

    Domain skills are silently skipped; pass a pre-filtered list or a mixed list.
    All matches are returned; there is no semantic ranking or top-k limit.
    """
    return [
        s
        for s in skills
        if s.skill_class == "system" and _is_applicable(s, phase=phase, category=category)
    ]


def _is_applicable(skill: ActiveSkill, *, phase: str | None, category: str | None) -> bool:
    if skill.always_apply:
        return True
    if skill.phase_scope is None:
        return False
    if phase is None or phase not in skill.phase_scope:
        return False
    if skill.category_scope is None:
        return True
    return category is not None and category in skill.category_scope
