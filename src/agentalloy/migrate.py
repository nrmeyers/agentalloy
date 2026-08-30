"""Migration CLI: ``python -m agentalloy.migrate``.

Ensures the unified OverGraph corpus store schema exists and keeps fragment
``phase_scope`` in sync with the authored ``skills.phase_scope``. Safe to run
multiple times (idempotent).
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

from agentalloy.config import get_settings
from agentalloy.storage.open import open_skills

if TYPE_CHECKING:
    from agentalloy.storage.protocols import SkillStore

logger = logging.getLogger(__name__)


def phase_scope_by_skill(store: SkillStore) -> dict[str, list[str] | None]:
    """Read authored ``phase_scope`` for every active skill from the skill store."""
    skills = store.get_active_skills()
    return {s.skill_id: (list(s.phase_scope) if s.phase_scope else None) for s in skills}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    settings.ensure_data_dirs()

    logger.info("migrate corpus store path=%s", settings.corpus_store_path)
    store = open_skills(settings, read_only=False)
    try:
        store.migrate()
        try:
            scope_by_skill = phase_scope_by_skill(store)
        except Exception:  # noqa: BLE001 — empty/fresh store has nothing to backfill
            scope_by_skill = {}
        if scope_by_skill:
            updated = store.backfill_phase_scope(scope_by_skill)
            logger.info("backfilled phase_scope on %d fragment row(s)", updated)
    finally:
        store.close()

    logger.info("migrate ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
