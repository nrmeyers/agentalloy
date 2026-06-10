"""Migration CLI: ``python -m agentalloy.migrate``.

Creates the LadybugDB graph store and the DuckDB vector + telemetry store
at the paths configured via environment. Safe to run multiple times
(idempotent).
"""

from __future__ import annotations

import logging
import sys

from agentalloy.config import get_settings
from agentalloy.storage.ladybug import LadybugStore
from agentalloy.storage.vector_store import open_or_create

logger = logging.getLogger(__name__)


def phase_scope_by_skill(store: LadybugStore) -> dict[str, list[str] | None]:
    """Read authored Skill.phase_scope for every active skill from the graph."""
    rows = store.execute(
        "MATCH (s:Skill) WHERE s.deprecated = false RETURN s.skill_id, s.phase_scope", {}
    )
    return {str(sid): (list(scope) if scope else None) for sid, scope in rows}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    settings.ensure_data_dirs()

    logger.info("migrate ladybug path=%s", settings.ladybug_db_path)
    scope_by_skill: dict[str, list[str] | None] = {}
    with LadybugStore(settings.ladybug_db_path) as store:
        store.migrate()
        try:
            scope_by_skill = phase_scope_by_skill(store)
        except Exception:  # noqa: BLE001 — empty/fresh graph has nothing to backfill
            scope_by_skill = {}

    logger.info("migrate duckdb path=%s", settings.duckdb_path)
    with open_or_create(settings.duckdb_path) as vs:
        # open_or_create runs the schema DDL + additive column migrations.
        if scope_by_skill:
            updated = vs.backfill_phase_scope(scope_by_skill)
            logger.info("backfilled phase_scope on %d fragment row(s)", updated)

    logger.info("migrate ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
