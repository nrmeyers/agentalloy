"""Internal read-path DTOs.

Frozen dataclasses — cheaper than Pydantic for hot reads; immutable on the bus
between retrieval, assembly, and the HTTP boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SkillClass = Literal["domain", "system", "workflow"]


@dataclass(frozen=True)
class ActiveSkill:
    skill_id: str
    canonical_name: str
    category: str
    skill_class: SkillClass
    domain_tags: list[str]
    always_apply: bool
    phase_scope: list[str] | None
    category_scope: list[str] | None
    active_version_id: str
    tier: str | None


@dataclass(frozen=True)
class ActiveFragment:
    fragment_id: str
    fragment_type: str
    sequence: int
    content: str
    skill_id: str
    version_id: str
    skill_class: SkillClass
    category: str
    domain_tags: list[str]
    # Authored Skill.phase_scope (e.g. ("build", "review")); None when the
    # skill declares no scope. Eligibility unions this with the category map.
    phase_scope: tuple[str, ...] | None = None
