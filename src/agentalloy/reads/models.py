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
    # Stage 0: the skill's one-line self-description. None when the corpus
    # predates the column or the author left it blank — read-tolerant.
    description: str | None = None


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
    # Authored Skill.phase_scope (e.g. ("build", "qa")); None when the
    # skill declares no scope. Eligibility unions this with the category map.
    phase_scope: tuple[str, ...] | None = None
    # Stage 0: the parent skill's one-line self-description (None-tolerant).
    description: str | None = None
    # Authored Skill.category_scope (e.g. ("process",) or ("framework",));
    # None when the corpus predates the projection or the author left it blank.
    # Drives process-class slot demotion in retrieval/domain.py.
    category_scope: tuple[str, ...] | None = None
