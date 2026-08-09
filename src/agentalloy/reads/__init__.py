"""Runtime read-path for active skills and fragments.

Every downstream retrieval (compose, direct retrieve, governance, inspection)
goes through this module. Non-active versions are filtered out by default.
"""

from __future__ import annotations

from agentalloy.reads.active import (
    InconsistentActiveVersionError,
    get_active_fragments,
    get_active_fragments_for_skill,
    get_active_skill_by_id,
    get_active_skills,
    get_active_version_by_id,
    get_deprecated_skill_ids,
)
from agentalloy.reads.models import ActiveFragment, ActiveSkill

__all__ = [
    "ActiveFragment",
    "ActiveSkill",
    "InconsistentActiveVersionError",
    "get_active_fragments",
    "get_active_fragments_for_skill",
    "get_active_skill_by_id",
    "get_active_skills",
    "get_active_version_by_id",
    "get_deprecated_skill_ids",
]
