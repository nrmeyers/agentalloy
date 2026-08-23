"""State leg — structured JSON context briefing injected every carrier turn.

The state leg gives the LLM a machine-readable snapshot of the current lifecycle
state: phase, active contract, artifact status, gate evaluation, and available
actions. It replaces the need for the LLM to run CLI commands to query state.

Designed for the stateless-phase model: a fresh agent picking up at any phase
boundary can read the state leg and immediately understand where things stand.
The query tool (``agentalloy_query``) provides deep-dive access to full artifact
bodies, decision rationale, and code-index lookups when the summary isn't enough.

Injection follows the banner pattern: built in the signal layer, stored on
``SignalResult.state_leg``, injected by the routers as strip-and-replace every
carrier turn with its own marker family (``AGENTALLOY-STATE``).
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def build_state_leg(
    phase: str,
    *,
    paused_mode: bool = False,
    store: Any = None,
    contract_id: str | None = None,
    gates_met: list[str] | None = None,
    gates_unmet: list[str] | None = None,
) -> str | None:
    """Build the structured state JSON for injection.

    Returns a JSON string ready for injection, or ``None`` when the state is
    too thin to be useful (no phase, no store). Soft: never raises — any
    failure in contract/artifact loading yields a minimal state with what's
    available rather than suppressing the entire leg.

    Parameters
    ----------
    phase:
        Current lifecycle phase.
    paused_mode:
        Whether the workflow is paused (``mode: paused`` in the store).
    store:
        ``DuckDBStateStore`` instance for loading contract and artifact data.
        ``None`` yields a minimal state (phase + mode only).
    contract_id:
        The active contract ID (from the signal layer's cursor). Used to load
        the contract summary and its artifacts.
    gates_met / gates_unmet:
        Gate names from the signal layer's evaluation. Surfaced so the LLM
        knows what's passing and what's blocking.
    """
    if not phase:
        return None

    state: dict[str, Any] = {
        "phase": phase,
        "mode": "paused" if paused_mode else "workflow",
    }

    if store is not None:
        _add_contract_state(state, store, contract_id, phase)

    _add_gate_status(state, gates_met, gates_unmet)
    _add_actions(state, phase, gates_unmet)

    return json.dumps(state, indent=2)


def _add_contract_state(
    state: dict[str, Any],
    store: Any,
    contract_id: str | None,
    phase: str,
) -> None:
    """Load contract summary and artifact status into the state dict.

    Soft: any failure leaves the state without contract data rather than raising.
    """
    if contract_id is None:
        return

    try:
        row = store.get_contract(contract_id)
    except Exception:
        logger.debug("state_leg: contract load failed for %s", contract_id, exc_info=True)
        return

    if row is None:
        return

    from agentalloy.contracts import contract_from_row

    try:
        contract = contract_from_row(row)
    except Exception:
        logger.debug("state_leg: contract_from_row failed for %s", contract_id, exc_info=True)
        return

    contract_summary: dict[str, Any] = {
        "slug": contract.task_slug,
        "domain_tags": contract.domain_tags,
    }

    if contract.scope.touches:
        contract_summary.setdefault("scope", {})["touches"] = contract.scope.touches
    if contract.scope.avoids:
        contract_summary.setdefault("scope", {})["avoids"] = contract.scope.avoids
    if contract.success_criteria:
        contract_summary["success_criteria"] = [
            c if isinstance(c, str) else str(c) for c in contract.success_criteria[:5]
        ]

    # Artifact status for this contract's slug across all phases
    artifacts = _load_artifact_status(store, contract.task_slug, phase)
    if artifacts:
        contract_summary["artifacts"] = artifacts

    state["contract"] = contract_summary


def _load_artifact_status(
    store: Any,
    slug: str,
    current_phase: str,
) -> dict[str, dict[str, Any]]:
    """Load artifact recording status for a contract slug.

    Returns a dict keyed by artifact name, with ``recorded`` (bool) and
    ``summary`` (first line of the artifact body, truncated) for each.
    """
    result: dict[str, dict[str, Any]] = {}

    # Check artifacts in the current phase
    try:
        rows = store.list_artifacts(current_phase, slug=slug, status="active")
    except Exception:
        logger.debug("state_leg: artifact list failed for slug=%s", slug, exc_info=True)
        return result

    for row in rows:
        name = row.get("name", "")
        content = row.get("content", "")
        if not name:
            continue
        summary = content.strip().split("\n")[0][:120] if content else ""
        result[name] = {
            "recorded": True,
            "summary": summary,
        }

    return result


def _add_gate_status(
    state: dict[str, Any],
    gates_met: list[str] | None,
    gates_unmet: list[str] | None,
) -> None:
    """Add gate evaluation status to the state dict."""
    met = gates_met or []
    unmet = gates_unmet or []

    if not met and not unmet:
        return

    state["gates"] = {
        "passing": met,
        "failing": unmet,
        "blocked": len(unmet) > 0,
    }


def _add_actions(
    state: dict[str, Any],
    phase: str,
    gates_unmet: list[str] | None,
) -> None:
    """Add available action hints based on current state.

    These are natural-language descriptions of what the LLM can do, not CLI
    commands. They tell the LLM about the marker convention and query tool
    without prescribing specific command strings.
    """
    actions: dict[str, str] = {}

    # Artifact recording is always available
    actions["record_artifact"] = (
        "Include an artifact marker in your response to record it, "
        "or describe the artifact naturally and the system will capture it."
    )

    # Contract recording is always available; it is how intake authors the first
    # downstream contract (there is no current contract to auto-propagate yet).
    actions["record_contract"] = (
        "To create the next phase's contract, include a contract marker in your "
        "response: <!-- agentalloy:contract phase=<phase> slug=<slug> route=<route> "
        ">...<!-- /agentalloy:contract -->. The body becomes the next phase's "
        "retrieval prompt; the system records it scoped to this repo."
    )

    # Phase advance depends on gate status
    unmet = gates_unmet or []
    if not unmet:
        if phase == "ship":
            actions["advance_phase"] = (
                "Ship is terminal — it does not self-advance. "
                "When the user confirms they're ready for the next work item, "
                "run `agentalloy phase set intake` to reset."
            )
        else:
            actions["advance_phase"] = (
                "State that the phase is complete. "
                "The phase advances automatically once exit gates pass."
            )
    else:
        actions["blocked"] = f"Phase cannot advance: {', '.join(unmet)} must be satisfied first."

    # Query tool is always available
    actions["query"] = (
        "Use the agentalloy_query tool for code search, symbol lookup, "
        "knowledge rationale, artifact bodies, or related decisions."
    )

    state["actions"] = actions
