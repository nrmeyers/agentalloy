"""Index-grounded validation of filled arguments (M1b).

Schema conformance is checked in ``protocol.parse_fill`` (structural). This
module answers the second, sharper question: *does the argument name something
the live index actually contains?* A 2.6B model fills plausible-looking but
nonexistent symbol names confidently; executing them just burns a step on
"No results" and pollutes the transcript. So the high-value lookups are
checked against the lifespan stores **before** execution:

* ``knowledge_why`` / ``symbols`` → the symbol must exist in the code index;
* ``knowledge_entities`` → the symbol must exist (the executor also accepts
  short-name matches, so validation uses the same resolver);
* ``artifact_body`` / ``contract_detail`` → the slug must be a known contract
  in the request-scoped state store.

Everything else (``code_search``, ``knowledge_related``, ``telemetry``,
``get_skill_for``, ``none``) is free-text / numeric and validates trivially —
an empty result there is a normal "No results", not a hallucination.

Every check is fail-open in the *opposite* direction from the LM stages: when
the store lookup itself fails (no code index, store not open, backend error)
validation **passes** and the executor gets to decide, so a flaky dependency
never blocks a question that schema validation already accepted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentalloy.local_agent.protocol import Action

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ValidationCheck:
    """Outcome of one index-grounded check; a failed ``error`` feeds the retry."""

    ok: bool
    error: str | None = None


class Validator:
    """Runs the index-grounded checks for one (action, args) pair.

    The loop calls :meth:`validate` once per fill; a failed check's message is
    fed back into the single fill retry, and the retry failing again degrades
    the step (fail-open, see ``loop.py``).
    """

    def __init__(
        self,
        *,
        code_index_state: Any | None = None,
        state_store: Any | None = None,
        repo_slug: str | None = None,
        repo_root: Path,
    ) -> None:
        # None = uncheckable: the corresponding checks pass through (fail-open)
        # and the executor decides.
        self._code_index_state = code_index_state
        self._state_store = state_store
        self._repo_slug = repo_slug
        self._repo_root = repo_root

    async def validate(self, action: Action, args: dict[str, Any]) -> ValidationCheck:
        """Return the check outcome; a failed check's ``error`` feeds the retry."""
        value: str | None
        if action in (Action.KNOWLEDGE_WHY, Action.SYMBOLS, Action.KNOWLEDGE_ENTITIES):
            value = args.get("query")
            if not isinstance(value, str) or not value.strip():
                return ValidationCheck(
                    False, "argument 'query' must be a fully-qualified symbol name"
                )
            return await self._check_symbol(value)
        if action is Action.ARTIFACT_BODY or action is Action.CONTRACT_DETAIL:
            value = args.get("slug")
            if not isinstance(value, str) or not value.strip():
                return ValidationCheck(False, "argument 'slug' must be a contract slug")
            return self._check_contract(value)
        # free-text / numeric / none — the schema check was enough
        return ValidationCheck(True)

    # ------------------------------------------------------------------
    # Code index
    # ------------------------------------------------------------------

    async def _check_symbol(self, fqn: str) -> ValidationCheck:
        if self._code_index_state is None:
            return ValidationCheck(True)  # no index on this service — executor decides
        try:
            from agentalloy.code_index.api.deps import with_handles  # noqa: PLC0415

            state = self._code_index_state
            slug = self._repo_slug
            if slug is None:
                slugs = sorted({r.slug for r in state.jobs.list_repos()})
                if not slugs:
                    return ValidationCheck(True)  # nothing indexed yet
                if len(slugs) > 1:
                    # Ambiguous scope — the executor rejects with its own
                    # message; pass validation so that path owns the answer.
                    return ValidationCheck(True)
                slug = slugs[0]
            # Mirror the executor's resolution exactly — validation must
            # query the same data directory (checkout) the executor will.
            indexed = state.jobs.get_repo(slug)
            if indexed is None:
                return ValidationCheck(True)  # not indexed — executor reports it

            def _found(h: Any) -> bool:
                if h.graph.symbols_by_name(fqn):
                    return True
                return h.graph.symbol(fqn) is not None

            found = await with_handles(state, slug, _found, repo_path=indexed.repo_path)
        except Exception:  # noqa: BLE001 — a broken lookup must not block the step
            logger.warning(
                "local agent symbol lookup failed for %r; passing validation through",
                fqn,
                exc_info=True,
            )
            return ValidationCheck(True)
        if not found:
            return ValidationCheck(
                False, f"argument 'query'={fqn!r} does not exist in the code index"
            )
        return ValidationCheck(True)

    # ------------------------------------------------------------------
    # State store
    # ------------------------------------------------------------------

    def _check_contract(self, slug: str) -> ValidationCheck:
        if self._state_store is None:
            return ValidationCheck(True)  # no state store — executor decides
        try:
            from agentalloy.api.state_router import scoped_state_store  # noqa: PLC0415

            store = scoped_state_store(self._state_store, self._repo_root)
            # Same single-slug lookup the /state routers (and the executor) use.
            known = store.get_contract(slug) is not None
        except Exception:  # noqa: BLE001
            logger.warning(
                "local agent contract lookup failed for %r; passing validation through",
                slug,
                exc_info=True,
            )
            return ValidationCheck(True)
        if not known:
            return ValidationCheck(False, f"argument 'slug'={slug!r} is not a known contract")
        return ValidationCheck(True)
