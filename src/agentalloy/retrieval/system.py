"""System-skill fragment retrieval pipeline (NXS-771).

No LLM, no embedding, no ranking. Evaluates applicability predicates against
phase/category context and returns all governance fragments from matching skills.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from agentalloy.applicability import filter_applicable_system_skills
from agentalloy.reads import get_active_fragments_for_skill, get_active_skills
from agentalloy.reads.models import ActiveFragment
from agentalloy.storage.protocols import SkillStore


@dataclass(frozen=True)
class SystemRetrievalResult:
    candidates: list[ActiveFragment]
    applied_skill_ids: list[str]
    retrieval_ms: int


def retrieve_system_fragments(
    store: SkillStore,
    *,
    phase: str | None,
    category: str | None,
) -> SystemRetrievalResult:
    """Return all governance fragments from applicable active system skills.

    Steps:
    1. Load all active system skills from the store.
    2. Filter by applicability predicate (phase_scope, category_scope, always_apply).
    3. Collect every fragment from each matching skill — no ranking or truncation.
    4. Return an empty result if nothing matches; never raises on empty.
    """
    start_ns = time.perf_counter_ns()

    active_skills = get_active_skills(store, skill_class="system")
    applicable = filter_applicable_system_skills(active_skills, phase=phase, category=category)

    applicable_sorted = sorted(applicable, key=lambda s: s.skill_id)
    applied_skill_ids = [s.skill_id for s in applicable_sorted]

    candidates: list[ActiveFragment] = []
    for skill in applicable_sorted:
        candidates.extend(get_active_fragments_for_skill(store, skill.skill_id))

    retrieval_ms = int((time.perf_counter_ns() - start_ns) // 1_000_000)
    return SystemRetrievalResult(
        candidates=candidates,
        applied_skill_ids=applied_skill_ids,
        retrieval_ms=retrieval_ms,
    )
