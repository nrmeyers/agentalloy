"""Reload the in-memory RuntimeCache after an in-process corpus write.

The service loads its RuntimeCache once at boot; a corpus write made from a
web endpoint (POST /api/reembed, the wizard's pack install) would otherwise
serve stale skills until the next restart. Call this after the write completes
and the store handle has reconnected (see ``DuckDBSkillStore.released``).

The CLI writer path doesn't need this: ``agentalloy reembed`` restarts the
service it stopped, and the restart reloads the cache.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def refresh_runtime_cache(app: Any) -> bool:
    """Best-effort cache reload + swap into the live orchestrators.

    Returns True on success. On any failure (degraded app state, a test app
    without the default lifespan, a load error) it returns False and the
    previous cache keeps serving — stale beats dead.
    """
    store = getattr(app.state, "store", None)
    if store is None:
        return False
    try:
        from agentalloy.runtime_state import load_runtime_cache

        runtime = load_runtime_cache(store)
    except Exception as exc:  # noqa: BLE001 — stale cache beats a dead service
        logger.warning("runtime cache refresh failed — serving the previous cache: %s", exc)
        return False
    app.state.runtime = runtime
    for attr in ("compose_orchestrator", "retrieve_orchestrator"):
        orch = getattr(app.state, attr, None)
        if orch is not None:
            orch.rebind_source(runtime)
    logger.info("runtime cache refreshed after in-process corpus write")
    return True
