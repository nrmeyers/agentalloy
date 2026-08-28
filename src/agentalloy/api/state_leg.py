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
from functools import lru_cache
from pathlib import Path
from typing import Any

from agentalloy.api.state_client import resolve_base_url
from agentalloy.code_index.slug import repo_slug
from agentalloy.storage.stream_id import resolve_stream_id

logger = logging.getLogger(__name__)


def build_state_leg(
    phase: str,
    *,
    paused_mode: bool = False,
    store: Any = None,
    contract_id: str | None = None,
    gates_met: list[str] | None = None,
    gates_unmet: list[str] | None = None,
    repo_root: Path | str | None = None,
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
    repo_root:
        The project root this panel describes. When given, a ``scope`` object
        is added so an agent hitting the service over HTTP can target the
        right bucket (state endpoints take ``?repo_root=``, code-index
        endpoints take ``?repo=<slug>``) instead of reverse-engineering it.
    """
    if not phase:
        return None

    state: dict[str, Any] = {
        "phase": phase,
        "mode": "paused" if paused_mode else "workflow",
    }

    scope = _add_scope(state, repo_root)

    if store is not None:
        _add_contract_state(state, store, contract_id, phase)
        slug = state.get("contract", {}).get("slug")
        if slug:
            _add_routed_findings(state, store, slug)

    _add_gate_status(state, gates_met, gates_unmet)
    _add_actions(state, phase, gates_unmet, scope)

    return json.dumps(state, indent=2)


@lru_cache(maxsize=256)
def _repo_slug_for(root: str) -> str:
    """Slug the repo root for the scope panel (cached — git probe per miss)."""
    return repo_slug(Path(root))


def _add_scope(state: dict[str, Any], repo_root: Path | str | None) -> dict[str, Any] | None:
    """Expose the active (repo, stream) scope this panel describes.

    The service serves every repo from one store; a call without the right
    scope lands in a different bucket and reads back empty.  An agent with no
    other view of the deployment had to reverse-engineer this — surface it.
    Returns the scope dict (also stored on ``state["scope"]``) so the action
    hints can quote its values verbatim.
    """
    if repo_root is None:
        return None
    root = Path(repo_root)
    scope: dict[str, Any] = {
        "repo_root": str(root),
        "repo": _repo_slug_for(str(root)),
        "stream_id": resolve_stream_id(root),
        "service": resolve_base_url(),
    }
    state["scope"] = scope
    return scope


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


def _add_actions(
    state: dict[str, Any],
    phase: str,
    gates_unmet: list[str] | None,
    scope: dict[str, Any] | None = None,
) -> None:
    """Add available action hints based on current state.

    These are natural-language descriptions of what the LLM can do, not CLI
    commands. They tell the LLM about the marker convention and query tool
    without prescribing specific command strings.
    """
    actions: dict[str, str] = {}

    # Artifact recording is always available — the store endpoint is the one
    # authoritative mechanism (marker extraction is off by default).
    actions["record_artifact"] = (
        "Record artifacts via the state service artifact endpoint "
        "(PUT /state/artifact). A file on disk is not an artifact — the "
        "artifact exists only once the PUT succeeds."
    )

    # Contract recording is always available; it is how intake authors the first
    # downstream contract (there is no current contract to auto-propagate yet).
    actions["record_contract"] = (
        "To create the next phase's contract, use the advance action (POST "
        "/state/advance writes the contract in the same request). For per-task "
        "build contracts during plan, use POST /contracts (scoped by "
        "?repo_root=). The contract body becomes the next phase's retrieval prompt."
    )

    # Phase advance depends on gate status
    unmet = gates_unmet or []
    if not unmet:
        if phase == "ship":
            actions["advance_phase"] = (
                "Ship is terminal — it does not self-advance. "
                "When the user confirms they're ready for the next work item, "
                "use the reset action below to return to intake."
            )
        else:
            actions["advance_phase"] = (
                "When the phase's work is complete, advance via the advance "
                "action below (POST /state/advance) — approval-gated phases "
                "need approved: true once the user approves."
            )
    else:
        actions["blocked"] = f"Phase cannot advance: {', '.join(unmet)} must be satisfied first."

    # Query tool is always available. When the scope is known, make the hint
    # self-sufficient: the tool is not in every harness's reachable tool set,
    # so the fallback (raw HTTP against the local service) must carry the
    # base URL and the scoping params inline — worked examples, no more.
    if scope is not None:
        service = scope["service"]
        # Artifact recording: the explicit PUT endpoint is the reliable path —
        # marker extraction is off by default, and the exit gates match on the
        # artifact NAME, so the hint must pin the exact names per phase.
        actions["record_artifact"] = (
            f"To record this phase's deliverable artifact, call "
            f"PUT {service}/state/artifact?repo_root={scope['repo_root']} "
            f"with JSON body: {{\"phase\": \"<phase>\", \"slug\": \"<task-slug>\", "
            f"\"name\": \"<artifact-name>\", \"content\": \"<markdown body>\"}}. "
            f"Exit gates match on the artifact name — use exactly: "
            f"spec → 'spec.artifact', design → 'approach.artifact', "
            f"plan → 'tasks.artifact' and 'test-plan.artifact', "
            f"sdd-fast → 'fast.artifact'. "
            f"For long bodies, write the JSON to a temp file and pass it with "
            f"curl --data @<file> — never inline multi-line JSON in shell quotes. "
            f"In a spec artifact, '## AC-N: <text>' headings are merged into the "
            f"contract's success criteria automatically."
        )
        actions["query"] = (
            "Deep-dive lookups beyond this summary (artifact bodies, contract "
            "detail, code search, symbol lookup, governing decisions): use the "
            "agentalloy_query tool when it is in your tool set; if it is not, "
            f"GET the service directly. Key endpoints (all scoped by ?repo_root={scope['repo_root']}):\n"
            f"  - List contracts: {service}/contracts?repo_root={scope['repo_root']}\n"
            f"  - Get artifact: {service}/state/artifact/{phase}/<slug>/<name>?repo_root={scope['repo_root']}\n"
            f"  - Get phase: {service}/state/phase?repo_root={scope['repo_root']}\n"
            f"/state/* and /contracts/* are scoped by ?repo_root=; /code/* "
            f"by ?repo={scope['repo']} (see the code_index action)."
        )
        # Code index / knowledge graph: semantic + lexical search, symbol lookup,
        # structural graph queries, and decision-doc (knowledge) retrieval.
        actions["code_index"] = (
            f"Code index / knowledge graph (all scoped by ?repo={scope['repo']}):\n"
            f"  - Semantic search: GET {service}/code/search/semantic?repo={scope['repo']}&q=<query>&k=10\n"
            f"  - Lexical (BM25) search: GET {service}/code/search/lexical?repo={scope['repo']}&q=<query>\n"
            f"  - Symbol lookup by FQN: GET {service}/code/search/symbol?repo={scope['repo']}&fqn=<fully.qualified.Name>\n"
            f"  - Graph queries: GET {service}/code/search/structural?repo={scope['repo']}"
            f"&query=<callers|callees|transitive_callers|governing_decisions|counts_by_kind>&fqn=<fqn>\n"
            f"  - Decision docs (why code exists): GET {service}/code/search/related-decisions?repo={scope['repo']}&q=<query>\n"
            f"  - Entity edges for a symbol: GET {service}/code/search/entities?repo={scope['repo']}&query=<symbol>\n"
            f"  - Budgeted task context: POST {service}/code/context-bundle "
            f"with JSON body: {{\"repo\": \"{scope['repo']}\", \"task\": \"<task description>\", \"budget_chars\": 24000}}\n"
            f"Use semantic search to find code, structural callers/callees to trace "
            f"impact, governing_decisions to find the rationale behind code."
        )
        # Phase advancement tool: single call to write contract + advance phase
        actions["advance"] = (
            f"To advance to the next phase, call POST {service}/state/advance?repo_root={scope['repo_root']} "
            f"with JSON body: {{\"slug\": \"<task-slug>\", \"contract_body\": \"<what the next phase needs to know>\", "
            f"\"to_phase\": \"<target-phase>\", \"route\": \"full\", \"approved\": true}}. "
            f"Set \"approved\": true when the user has approved the presented work — it records the "
            f"approval and advances in one call (standalone: POST {service}/state/approve-phase). "
            f"Approval-gated phases (spec, design) block until approved; approval is refused until the "
            f"phase's exit artifact is recorded, and editing an approved artifact voids the approval."
        )
        # Contract retrieval: pull a specific contract by ID or list all
        actions["contracts"] = (
            f"To retrieve contracts:\n"
            f"  - List all: GET {service}/contracts?repo_root={scope['repo_root']}\n"
            f"  - Get by ID: GET {service}/contracts/<contract_id>?repo_root={scope['repo_root']} "
            f"(contract_id format: '<phase>/<slug>', e.g., 'spec/test-feature')\n"
            f"  - Filter by phase: GET {service}/contracts?repo_root={scope['repo_root']}&phase=<phase>\n"
            f"Use this to pull the current contract before starting phase work."
        )
        # Session management: list, stash, resume, archive, cancel
        actions["sessions"] = (
            f"To manage workflow sessions (all POSTs take JSON body: "
            f"{{\"session_key\": \"<session-id>\"}}):\n"
            f"  - List active: GET {service}/state/sessions/active?repo_root={scope['repo_root']}\n"
            f"  - Stash (park work-in-progress to resume later): "
            f"POST {service}/state/sessions/stash?repo_root={scope['repo_root']}\n"
            f"  - Resume (bring a stashed session and its contracts back): "
            f"POST {service}/state/sessions/resume?repo_root={scope['repo_root']}\n"
            f"  - Archive (work item done, reached product — terminal): "
            f"POST {service}/state/sessions/archive?repo_root={scope['repo_root']}\n"
            f"  - Cancel (work item abandoned, never reached product — terminal): "
            f"POST {service}/state/sessions/cancel?repo_root={scope['repo_root']}\n"
            f"Use stash/resume to park and restore work-in-progress; "
            f"archive/cancel only when the work item is finished or abandoned."
        )
        # Reset to intake: start a new work item or abandon a stuck one.
        # Resets are backward moves — the exit gate does not guard them.
        actions["reset"] = (
            f"To reset the workflow back to intake (start a new work item, or "
            f"abandon a stuck/finished one): POST {service}/state/phase?repo_root={scope['repo_root']} "
            f"with JSON body: {{\"value\": \"intake\"}}. Resets are not gated. "
            f"Only reset when the user confirms the current work item is done or abandoned."
        )
    else:
        actions["query"] = (
            "Use the agentalloy_query tool for code search, symbol lookup, "
            "knowledge rationale, artifact bodies, or related decisions."
        )

    state["actions"] = actions
