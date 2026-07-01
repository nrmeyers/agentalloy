"""Migration CLI: ``python -m agentalloy.migrate``.

Ensures the DuckDB skill store (``agentalloy.duck``) schema exists and keeps the
Lance ``fragments`` dataset's ``phase_scope`` column in sync with the authored
``skills.phase_scope``. Safe to run multiple times (idempotent).
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

from agentalloy.config import get_settings
from agentalloy.storage.open import open_fragments, open_skills

if TYPE_CHECKING:
    from agentalloy.storage.protocols import SkillStore

logger = logging.getLogger(__name__)


def phase_scope_by_skill(store: SkillStore) -> dict[str, list[str] | None]:
    """Read authored ``phase_scope`` for every active skill from the skill store."""
    rows = store.execute("SELECT skill_id, phase_scope FROM skills WHERE deprecated = false")
    return {str(sid): (list(scope) if scope else None) for sid, scope in rows}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    settings.ensure_data_dirs()

    logger.info("migrate skills path=%s", settings.duckdb_path)
    store = open_skills(settings, read_only=False)
    scope_by_skill: dict[str, list[str] | None] = {}
    try:
        store.migrate()
        try:
            scope_by_skill = phase_scope_by_skill(store)
        except Exception:  # noqa: BLE001 — empty/fresh store has nothing to backfill
            scope_by_skill = {}
    finally:
        store.close()

    logger.info("migrate fragments path=%s", settings.fragments_lance_path)
    fragments = open_fragments(settings)
    try:
        if scope_by_skill:
            updated = fragments.backfill_phase_scope(scope_by_skill)
            logger.info("backfilled phase_scope on %d fragment row(s)", updated)
    finally:
        fragments.close()

    logger.info("migrate ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
