"""State leg — structured JSON context briefing injected every carrier turn.

The state leg gives the LLM a machine-readable snapshot of the current
lifecycle state: phase, active contract, artifact status, gate evaluation,
and available actions. It replaces the need for the LLM to run CLI commands
to query state.

Designed for the stateless-phase model: a fresh agent picking up at any
phase boundary can read the state leg and immediately understand where
things stand.

Action hints quote the v2 service surface verbatim — ``POST /tool`` with a
tool name, JSON-string args, and the ``project`` scope key, plus the
``GET /status`` / ``GET /gates`` reads — so a hint is a ready-to-send
request, not a paraphrase the LLM has to reconstruct.

Injection follows the banner pattern: built in the signal layer, stored on
``SignalResult.state_leg``, injected by the routers as strip-and-replace
every carrier turn with its own marker family (``AGENTALLOY-STATE``).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from agentalloy.api.state_client import resolve_base_url
from agentalloy.registry import project_key

logger = logging.getLogger(__name__)


def build_state_leg(
    phase: str,
    *,
    paused_mode: bool = False,
    store: Any = None,
    contract_id: str | None = None,
    gates_met: list[str] | None = None,
    gates_unmet: list[str] | None = None,
    project_root: Path | str | None = None,
) -> str | None:
    """Build the structured state JSON for injection.

    Returns a JSON string ready for injection, or ``None`` when the state is
    too thin to be useful (no phase). Soft: never raises — any failure in
    contract/artifact loading yields a minimal state with what's available
    rather than suppressing the entire leg.

    Parameters
    ----------
    phase:
        Current lifecycle phase.
    paused_mode:
        Whether the workflow is paused (``mode: paused`` in the store).
    store:
        State store instance for loading contract and artifact data.
        ``None`` yields a minimal state (phase + mode only).
    contract_id:
        The active contract ID (from the signal layer's cursor). Used to load
        the contract summary and its artifacts.
    gates_met / gates_unmet:
        Gate names from the signal layer's evaluation. Surfaced so the LLM
        knows what's passing and what's blocking.
    project_root:
        The project root this panel describes. When given, a ``scope`` object
        is added (service base URL + ``project`` key) so an agent hitting the
        service over HTTP targets the right bucket — every ``/tool`` body and
        every ``?project=`` read must carry it.
    """
    if not phase:
        return None

    state: dict[str, Any] = {
        "phase": phase,
        "mode": "paused" if paused_mode else "workflow",
    }

    scope = _add_scope(state, project_root)

    if store is not None:
        _add_contract_state(state, store, contract_id, phase)
        slug = state.get("contract", {}).get("slug")
        if slug:
            _add_routed_findings(state, store, slug)

    _add_gate_status(state, gates_met, gates_unmet)
    _add_actions(state, phase, gates_unmet, scope)

    return json.dumps(state, indent=2)


def _add_scope(state: dict[str, Any], project_root: Path | str | None) -> dict[str, Any] | None:
    """Expose the (service, project) scope this panel describes.

    The v2 service serves every project from one store; a ``/tool`` call or
    a ``?project=`` read without the right key lands in a different bucket
    and reads back empty. An agent with no other view of the deployment had
    to reverse-engineer this — surface it. Returns the scope dict (also
    stored on ``state["scope"]``) so the action hints can quote its values
    verbatim.
    """
    if project_root is None:
        return None
    scope: dict[str, Any] = {
        "service": resolve_base_url(),
        "project": project_key(project_root),
    }
    state["scope"] = scope
    return scope


def _tool_body(project: str, name: str, args: dict[str, Any]) -> str:
    """Render the exact ``POST /tool`` body for a tool call.

    The service parses ``args`` with ``json.loads``, so the tool arguments
    are a JSON *string* inside the body — double-encoded on purpose. The
    rendered text is the wire format; the agent fills in only the
    ``<placeholders>``.
    """
    return json.dumps(
        {"name": name, "args": json.dumps(args, separators=(",", ":")), "project": project},
        separators=(",", ":"),
    )


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


def _extract_routed_findings(content: str) -> list[str]:
    """Parse ``## Routed Findings`` entries from a QA artifact body.

    Returns a list of individual finding blocks (markdown strings).  Each
    finding starts with a ``###`` heading inside the ``## Routed Findings``
    section and extends to the next ``###`` or ``##`` heading.  Returns an
    empty list when the section is absent or has no entries.
    """
    lines = content.split("\n")
    in_section = False
    findings: list[str] = []
    current: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped == "## Routed Findings":
            in_section = True
            continue
        if in_section and stripped.startswith("## ") and not stripped.startswith("### "):
            break
        if not in_section:
            continue
        if stripped.startswith("### "):
            if current:
                findings.append("\n".join(current).strip())
            current = [line]
        elif in_section:
            if current:
                current.append(line)

    if current:
        findings.append("\n".join(current).strip())

    return findings


def _add_routed_findings(
    state: dict[str, Any],
    store: Any,
    slug: str,
) -> None:
    """Surface QA-routed findings in the state leg when present.

    Loads the QA artifact for *slug*, parses its ``## Routed Findings``
    section, and adds the entries as ``state["routed_findings"]``.  Soft:
    any failure leaves the state without routed findings rather than
    raising.  The section's absence (clean QA report) means no key is
    added — the receiving phase sees nothing and proceeds normally.
    """
    try:
        rows = store.list_artifacts("qa", slug=slug, name_glob="*.artifact")
    except Exception:
        logger.debug("state_leg: qa artifact load failed for slug=%s", slug, exc_info=True)
        return

    if not rows:
        return

    content = rows[0].get("content", "")
    if not content:
        return

    findings = _extract_routed_findings(content)
    if findings:
        state["routed_findings"] = findings


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


def _next_phase_info(phase: str) -> tuple[str | None, bool]:
    """(next phase, is the outgoing transition approval-gated).

    Lazy imports: this module sits on the proxy's hot path and must not pull
    the phase graph (langgraph) at import time.
    """
    try:
        from agentalloy.phase_machine import APPROVAL_GATES
        from agentalloy.state_store import PHASE_ORDER
    except Exception:
        logger.debug("state_leg: lifecycle import failed", exc_info=True)
        return None, False
    if phase not in PHASE_ORDER:
        return None, False
    idx = PHASE_ORDER.index(phase)
    if idx + 1 >= len(PHASE_ORDER):
        return None, False
    return PHASE_ORDER[idx + 1], f"{phase}→{PHASE_ORDER[idx + 1]}" in APPROVAL_GATES


def _add_actions(
    state: dict[str, Any],
    phase: str,
    gates_unmet: list[str] | None,
    scope: dict[str, Any] | None = None,
) -> None:
    """Add available action hints for the v2 service surface.

    Every hint quotes a ready-to-send request — a ``POST /tool`` body (tool
    name + JSON-string args + project key) or a ``GET``/``POST`` to a route —
    so the LLM never has to reconstruct the wire format from memory.
    """
    actions: dict[str, str] = {}

    # Phase advance depends on gate status.
    unmet = gates_unmet or []
    if unmet:
        actions["blocked"] = f"Phase cannot advance: {', '.join(unmet)} must be satisfied first."

    if scope is not None:
        service = scope["service"]
        project = scope["project"]
        next_phase, approval_gated = _next_phase_info(phase)

        # Artifact recording is always available — the tool is the one
        # authoritative mechanism (marker extraction is off by default).
        actions["record_artifact"] = (
            "Record this phase's exit artifact — the advance gate reads the "
            f"'{phase}-exit' row and a file on disk is not an artifact:\n"
            f"  POST {service}/tool with body "
            f"{_tool_body(project, 'artifact_record', {'phase': phase, 'name': f'{phase}-exit', 'body': '<the deliverable>'})}\n"
            "Replace <the deliverable> with the artifact's markdown; the artifact exists only once the call returns ok."
        )

        # Contract recording is always available; it is how intake authors
        # the first downstream contract (there is no current contract to
        # auto-propagate yet).
        actions["record_contract"] = (
            "Author the next phase's contract — its body becomes that phase's "
            "retrieval prompt:\n"
            f"  POST {service}/tool with body "
            f"{_tool_body(project, 'contract_add', {'slug': '<task-slug>', 'domain_tags': ['<stack>'], 'touches': '<files that phase will touch>'})}"
        )

        if not unmet:
            if phase == "ship":
                actions["advance_phase"] = (
                    "Ship is terminal — it does not self-advance. "
                    "When the user confirms they're ready for the next work item, "
                    "use the reset action below to return to intake."
                )
            elif next_phase is None:
                actions["advance_phase"] = (
                    "This phase is terminal — it does not self-advance."
                )
            else:
                hint = (
                    f"Advance to {next_phase} once the phase's work and its "
                    f"'{phase}-exit' artifact are recorded:\n"
                    f"  POST {service}/tool with body "
                )
                if approval_gated:
                    hint += (
                        f"{_tool_body(project, 'phase_advance', {'target': next_phase, 'approved': True})}\n"
                        "Set approved to true ONLY once the user has explicitly "
                        "approved the presented work — the gate refuses without it."
                    )
                else:
                    hint += (
                        f"{_tool_body(project, 'phase_advance', {'target': next_phase})}"
                    )
                actions["advance_phase"] = hint

        # Read-only lookups. When the scope is known, make the hint
        # self-sufficient: the MCP tool is not in every harness's reachable
        # set, so the fallback (raw HTTP against the local service) carries
        # the base URL and project key inline — worked examples, no more.
        actions["query"] = (
            "Read-only lookups (no state writes):\n"
            f"  - Lifecycle: GET {service}/status?project={project} (phase + next gate) | "
            f"GET {service}/gates?project={project} (every transition gate)\n"
            f"  - Tools: POST {service}/tool with one of these bodies (fill the <placeholders>):\n"
            f"    {_tool_body(project, 'code_search', {'query': '<symptom or term>', 'k': 8})}\n"
            f"    {_tool_body(project, 'symbols', {'fqn': '<module.Func>'})}\n"
            f"    {_tool_body(project, 'knowledge_why', {'fqn': '<module.Func>'})}\n"
            f"    {_tool_body(project, 'knowledge_related', {'query': '<decision or topic>'})}\n"
            f"    {_tool_body(project, 'artifact_body', {'phase': phase, 'name': '<artifact-name>'})}\n"
            f"    {_tool_body(project, 'contract_detail', {'slug': '<task-slug>'})}\n"
            f"    {_tool_body(project, 'get_skill_for', {'task': '<what you are doing>', 'phase': phase})}"
        )

        # Session management: list, detail, stash, resume, archive, cancel
        actions["sessions"] = (
            "Work-in-progress sessions — park and restore, or close a work item:\n"
            f"  - List: GET {service}/sessions | Detail: GET {service}/sessions/<session_key>\n"
            f"  - Stash: POST {service}/sessions/<session_key>/stash | Resume: POST {service}/sessions/<session_key>/resume\n"
            f"  - Archive (work item done): POST {service}/sessions/<session_key>/archive\n"
            f"  - Cancel (abandoned): POST {service}/sessions/<session_key>/cancel\n"
            "Use stash/resume to park and restore work-in-progress; "
            "archive/cancel only when the work item is finished or abandoned."
        )

        # Reset to intake: start a new work item or abandon a stuck one.
        # Resets are backward moves — the exit gate does not guard them.
        actions["reset"] = (
            "Operator-only (the user must confirm the work item is done or "
            "abandoned): reset the lifecycle to intake and clear approvals — "
            "contracts and artifacts are kept:\n"
            f"  POST {service}/tool with body "
            f"{_tool_body(project, 'phase_reset', {})}"
        )
    else:
        actions["query"] = (
            "Use the agentalloy MCP tools (code_search, contract_detail, ...) "
            "for code search, symbol lookup, knowledge rationale, or artifact "
            "bodies."
        )

    state["actions"] = actions
