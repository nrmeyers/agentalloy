"""NXS-770: system-skill applicability predicate logic."""

from __future__ import annotations

from agentalloy.applicability import filter_applicable_system_skills
from agentalloy.reads.models import ActiveSkill


def _skill(
    skill_id: str,
    *,
    always_apply: bool = False,
    phase_scope: list[str] | None = None,
    category_scope: list[str] | None = None,
    skill_class: str = "system",
) -> ActiveSkill:
    return ActiveSkill(
        skill_id=skill_id,
        canonical_name=skill_id,
        category="ops",
        skill_class=skill_class,  # type: ignore[arg-type]
        domain_tags=[],
        always_apply=always_apply,
        phase_scope=phase_scope,
        category_scope=category_scope,
        active_version_id="v1",
        tier=None,
    )


# AC-1
def test_always_apply_included_regardless_of_phase_and_category() -> None:
    skill = _skill("always", always_apply=True)
    result = filter_applicable_system_skills([skill], phase=None, category=None)
    assert [s.skill_id for s in result] == ["always"]


# AC-2: phase match, no category restriction
def test_phase_match_no_category_restriction_included() -> None:
    skill = _skill("phase-only", phase_scope=["setup"])
    result = filter_applicable_system_skills([skill], phase="setup", category=None)
    assert [s.skill_id for s in result] == ["phase-only"]


# AC-3: both phase and category match
def test_phase_and_category_both_match_included() -> None:
    skill = _skill("scoped", phase_scope=["execution"], category_scope=["onboarding"])
    result = filter_applicable_system_skills([skill], phase="execution", category="onboarding")
    assert [s.skill_id for s in result] == ["scoped"]


# AC-3: phase matches but category does not
def test_phase_matches_but_category_does_not_excluded() -> None:
    skill = _skill("scoped", phase_scope=["execution"], category_scope=["onboarding"])
    result = filter_applicable_system_skills([skill], phase="execution", category="compliance")
    assert result == []


# AC-1 edge: no phase_scope, no always_apply → excluded
def test_no_phase_scope_no_always_apply_excluded() -> None:
    skill = _skill("unscoped")
    result = filter_applicable_system_skills([skill], phase="setup", category=None)
    assert result == []


# AC-2 edge: phase mismatch
def test_phase_mismatch_excluded() -> None:
    skill = _skill("other-phase", phase_scope=["verification"])
    result = filter_applicable_system_skills([skill], phase="setup", category=None)
    assert result == []


# AC-4: multiple applicable — all included, no top-k limit
def test_multiple_applicable_all_included_no_limit() -> None:
    skills = [
        _skill("a", always_apply=True),
        _skill("b", phase_scope=["setup"]),
        _skill("c", phase_scope=["setup"], category_scope=["ops"]),
        _skill("d", phase_scope=["execution"]),  # phase mismatch → excluded
    ]
    result = filter_applicable_system_skills(skills, phase="setup", category="ops")
    assert {s.skill_id for s in result} == {"a", "b", "c"}


# Domain skills silently skipped
def test_domain_skills_excluded_even_with_always_apply() -> None:
    domain = _skill("dom", always_apply=True, skill_class="domain")
    result = filter_applicable_system_skills([domain], phase=None, category=None)
    assert result == []


# AC-3 edge: category_scope set but request has no category
def test_category_scope_set_but_request_has_no_category_excluded() -> None:
    skill = _skill("cat-required", phase_scope=["setup"], category_scope=["ops"])
    result = filter_applicable_system_skills([skill], phase="setup", category=None)
    assert result == []
