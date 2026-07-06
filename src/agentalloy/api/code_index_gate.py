"""Availability gate for the ``sys-code-index`` system skill.

``sys-code-index`` teaches the agent to PULL from the local code index
(``agentalloy code ...``). That prose is only true when the request's repo is
actually indexed — injecting it into an unindexed repo sends the agent to a
CLI that errors out. The capability layer (``provides`` /
``requires_capability``) is deliberately deferred by design
(docs/design/sdd-golden-skills.md), so this module implements the one
runtime-conditional system skill as a proxy-side drop-by-skill_id filter at
the compose boundary: the proxy resolves the repo per request, asks
:func:`code_index_available`, and passes :func:`system_skill_exclusions` into
the Tier 1 system compose.

Fail-closed doctrine: any doubt (module off, no repo identity, registry
missing or unreadable, slug derivation failure) means DROP — a missing hint is
harmless, a wrong hint wastes the agent's turn. The probe never raises into
the compose path.

Import doctrine: ``agentalloy.code_index`` must not be imported while the
module is disabled (its heavy deps ship behind an extra), so the registry
lookup is lazy-imported only after the ``code_index_enabled`` check passes.
"""

from __future__ import annotations

import logging
from pathlib import Path

from agentalloy.config import Settings, get_settings

logger = logging.getLogger(__name__)

CODE_INDEX_SKILL_ID = "sys-code-index"


def code_index_available(repo: str | None, settings: Settings | None = None) -> bool:
    """True iff the code-index module is on AND ``repo`` has a completed index.

    One cheap sqlite read against the shared ``indexed_repos`` registry
    (jobs.sqlite). ``last_indexed_at`` must be set — an enrolled-but-never-
    indexed repo does not count. Never raises; every failure path returns
    False (fail-closed, see module docstring).
    """
    try:
        s = settings or get_settings()
        if not s.code_index_enabled or not repo:
            return False

        registry_path = Path(s.code_index_data_dir) / "jobs.sqlite"
        if not registry_path.exists():
            return False

        # Lazy imports: nothing under agentalloy.code_index may be imported
        # while the module toggle is off (see agentalloy/code_index/__init__).
        from agentalloy.code_index.slug import repo_slug
        from agentalloy.code_index.store.jobs_store import CodeIndexJobsStore

        slug = repo_slug(Path(repo))
        store = CodeIndexJobsStore(registry_path)
        try:
            record = store.get_repo(slug)
            return record is not None and record.last_indexed_at is not None
        finally:
            store.close()
    except Exception:
        logger.warning(
            "code-index availability probe failed for repo %r -- dropping %s",
            repo,
            CODE_INDEX_SKILL_ID,
            exc_info=True,
        )
        return False


def system_skill_exclusions(repo: str | None, settings: Settings | None = None) -> frozenset[str]:
    """The system skill_ids to drop from a compose for ``repo``.

    Empty when the code index is available; ``{sys-code-index}`` otherwise.
    The orchestrator applies this as a post-retrieval filter on the system leg.
    """
    if code_index_available(repo, settings):
        return frozenset()
    return frozenset({CODE_INDEX_SKILL_ID})
