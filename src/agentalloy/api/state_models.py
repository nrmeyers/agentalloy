"""Pydantic models for the state endpoint router.

Request and response shapes for POST /state/<kind>, GET /state/<kind>,
and the contract CRUD endpoints under /contracts.
Handler implementations bind to these types; the router uses them for
validation and OpenAPI documentation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

# All state kinds the service manages.
ALL_KINDS: frozenset[str] = frozenset(
    {
        "phase",
        "cursor",
        "announced",
        "composed",
        "approved",
        "banner-turns",
        "pause-reminded",
    },
)

# Kinds that participate in lease-based concurrency control.
LEASED_KINDS: frozenset[str] = frozenset({"phase", "approved"})

# Valid contract statuses stored in sdd_contract.
ContractStatus = Literal["active", "archived", "superseded"]

# Valid contract route values.
ContractRoute = Literal["full", "sdd-fast", "add-skill"]

# SDD phases that a contract may belong to.
ContractPhase = Literal["intake", "spec", "design", "plan", "build", "qa", "ship"]


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class StateWriteRequest(BaseModel):
    """Input to POST /state/<kind>.

    ``value`` is required and must be a non-empty string.
    ``session_key`` is optional — when omitted the write is repo-scoped.
    ``owner`` is optional — when provided it is recorded as the row owner
    (used for lease tracking on leased kinds).
    """

    value: Annotated[str, Field(min_length=1)]
    session_key: str | None = None
    owner: str | None = None


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class StateWriteResponse(BaseModel):
    """Success response for POST /state/<kind>.

    ``success`` is always True when the endpoint returns 200.
    ``conflict`` is present (and non-null) when a lease held by another
    session blocked the write — the caller gets a 409 in that case.
    ``lease_expires_at`` is present when the kind is leased and the write
    established a new lease.
    """

    success: Literal[True] = True
    kind: str
    value: str
    owner: str | None = None
    lease_expires_at: datetime | None = None
    conflict: StateConflictInfo | None = None


class StateConflictInfo(BaseModel):
    """Conflict detail returned when a lease is held by another session.

    ``lease_expires_at`` is a :class:`datetime` — Pydantic serialises it as
    an ISO-8601 string in the JSON response, so the wire format is unchanged
    from pre-branch behaviour.  The type was temporarily ``str`` during
    slice-01 development but that was a breaking API change that shipped
    unremarked; reverting to ``datetime`` restores type consistency with
    the internal :class:`~agentalloy.storage.state_store.LeaseConflict`
    dataclass.
    """

    owner: str
    lease_expires_at: datetime
    message: str


class StateReadResponse(BaseModel):
    """Success response for GET /state/<kind>.

    Returns ``null`` (not found) when no value has been written.
    """

    kind: str
    value: str | None = None


class StateAllResponse(BaseModel):
    """Success response for GET /state/all — all known kinds with their values."""

    state: dict[str, str]


class PhaseReadResponse(StateReadResponse):
    """Success response for GET /state/phase — the whole decoded phase row.

    A superset of :class:`StateReadResponse`: ``value`` still carries the bare
    phase name, so a caller that only wants "which phase" is unchanged.  The
    remaining fields exist because ``phase`` is the one kind whose stored row is
    a blob, and the CLI genuinely renders all of it — ``workflow status`` needs
    ``mode``/``paused_since``, ``phase get`` prints the timestamps, and
    ``phase set`` needs the prior ``transitioned_by`` to decide whether a write
    is a real transition.  Serving only ``value`` forced every one of those back
    onto the file mirror.

    Every field but ``kind``/``value`` is ``None`` when no phase is recorded.
    ``workflow`` is derived (``sdd-<phase>``), never caller-supplied.
    """

    mode: str | None = None
    paused_since: str | None = None
    transitioned_by: str | None = None
    started_at: str | None = None
    last_updated: str | None = None
    workflow: str | None = None
    phase_start_ref: str | None = None


# ---------------------------------------------------------------------------
# Phase advance with optional contract
# ---------------------------------------------------------------------------


class PhaseAdvanceRequest(BaseModel):
    """Input to POST /state/phase — phase value with an optional contract payload.

    When ``contract`` is provided, the phase write and contract write are
    committed inside a single ``store.transaction()``.  Validation failures
    in the contract payload roll back the entire transaction.
    """

    value: Annotated[str, Field(min_length=1, description="The new phase value")]
    owner: str | None = Field(
        default=None,
        description=(
            "Session owner for lease tracking.  Required when advancing to a leased phase."
        ),
    )
    contract: ContractCreateRequest | None = Field(
        default=None,
        description=(
            "Optional contract to store alongside the phase advance.  Both "
            "writes commit or roll back together."
        ),
    )
    mode: str | None = Field(
        default=None,
        description=(
            "Workflow mode ('paused' or empty to clear).  Omit to carry the stored "
            "value forward — only `agentalloy workflow pause/resume` sets it."
        ),
    )
    paused_since: str | None = Field(
        default=None,
        description="ISO timestamp pause was entered; empty string clears it.",
    )
    actor: str | None = Field(
        default=None,
        description=(
            "Session key credited with the transition.  Recorded only when the "
            "phase actually changes, so a different session can tell the phase "
            "moved and that it was not the one that moved it."
        ),
    )
    override: bool = Field(
        default=False,
        description=(
            "When True, bypass the phase exit gate.  Note: phases in "
            "_ALWAYS_APPROVAL_PHASES (spec, design, add-skill) are always "
            "refused — override has no effect on them."
        ),
    )


# ---------------------------------------------------------------------------
# Contract models
# ---------------------------------------------------------------------------


class ContractCreateRequest(BaseModel):
    """Input to POST /contracts (and the optional contract inside PhaseAdvanceRequest)."""

    contract_id: Annotated[str, Field(min_length=1, description="Unique contract identifier")]
    phase: ContractPhase = Field(description="SDD phase the contract belongs to")
    slug: Annotated[str, Field(min_length=1, description="Contract slug")]
    work_item: str | None = Field(default=None, description="Work item identifier")
    route: ContractRoute | None = Field(default=None, description="Route type")
    domain_tags: list[str] | None = Field(default=None, description="Domain tag list")
    scope_touches: list[str] | None = Field(default=None, description="Files the contract touches")
    scope_avoids: list[str] | None = Field(default=None, description="Files the contract avoids")
    success_criteria: list[str | dict[str, Any]] | None = Field(
        default=None,
        description="Success criteria list of strings or {id, text} dicts",
    )
    body: str | None = Field(default=None, description="Contract body (markdown)")


class ContractPatchRequest(BaseModel):
    """Input to PATCH /contracts/{id} — in-place correction."""

    body: str | None = None
    domain_tags: list[str] | None = None
    scope_touches: list[str] | None = None
    scope_avoids: list[str] | None = None
    success_criteria: list[str | dict[str, Any]] | None = None


class ContractSupersedeRequest(BaseModel):
    """Input to POST /contracts/{id}/supersede — fork a new revision."""

    new_contract_id: Annotated[str, Field(min_length=1)]
    phase: ContractPhase
    slug: Annotated[str, Field(min_length=1)]
    work_item: str | None = None
    route: ContractRoute | None = None
    domain_tags: list[str] | None = None
    scope_touches: list[str] | None = None
    scope_avoids: list[str] | None = None
    success_criteria: list[str | dict[str, Any]] | None = None
    body: str | None = None


class ContractResponse(BaseModel):
    """Response for GET /contracts/{id}."""

    contract_id: str
    phase: str
    slug: str
    work_item: str | None
    route: str | None
    domain_tags: list[str] | None
    scope_touches: list[str] | None
    scope_avoids: list[str] | None
    success_criteria: list[str | dict[str, Any]] | None
    status: str
    supersedes: str | None
    created_at: str
    updated_at: str
    body: str | None


class ContractListResponse(BaseModel):
    """Response for GET /contracts."""

    contracts: list[ContractResponse]


class ArtifactSetRequest(BaseModel):
    """Input to PUT /state/artifact — upsert a deliverable artifact body."""

    phase: ContractPhase
    slug: Annotated[str, Field(min_length=1)]
    name: Annotated[str, Field(min_length=1, description="e.g. 'spec.md', 'approach.md'")]
    content: str


class ArtifactResponse(BaseModel):
    """Response for artifact read/write endpoints."""

    phase: str
    slug: str
    name: str
    content: str | None
    updated_at: str


class ArtifactListResponse(BaseModel):
    """Response for GET /state/artifact."""

    artifacts: list[ArtifactResponse]


class PhaseAdvanceResponse(BaseModel):
    """Response for POST /state/phase when a contract is included.

    ``end_session_instruction`` is the deterministic end-of-phase directive
    surfaced identically by CLI, web, and proxy (D9).  Present on every
    successful phase advance.

    ``gate_verdict`` is present when the route evaluated an exit gate.
    It carries the result (``met``, ``not_met``, ``unknown``), the blocking
    reason, and a list of missing-artifact paths so the CLI can render
    operator-facing guidance without re-evaluating the gate itself.

    ``success`` is False when the gate blocked the write (``gate_verdict``
    carries the reason) — a blocked advance must never report success (#501).
    """

    success: bool = True
    kind: str
    value: str
    owner: str | None = None
    lease_expires_at: datetime | None = None
    conflict: StateConflictInfo | None = None
    contract_id: str | None = None
    end_session_instruction: str | None = None
    gate_verdict: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Resume response
# ---------------------------------------------------------------------------


class ResumeContractInfo(BaseModel):
    """Contract info inside a resume response."""

    contract_id: str
    phase: str
    slug: str
    domain_tags: list[str] | None = None
    scope_touches: list[str] | None = None
    scope_avoids: list[str] | None = None
    body: str | None = None


class ResumeResponse(BaseModel):
    """Response for GET /state/resume — assembled server-side for cold-session bootstrap.

    Contains the current phase, the cursor'd work-item with its tags and scope,
    the artifacts that phase owes, and governing decisions for scope.touches.
    """

    phase: str | None = None
    cursor_contract: ResumeContractInfo | None = None
    owed_artifacts: list[str] | None = None
    governing_decisions: list[str] | None = None
