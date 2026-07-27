"""Pydantic models for the state endpoint router.

Request and response shapes for POST /state/<kind>, GET /state/<kind>,
and the contract CRUD endpoints under /contracts.
Handler implementations bind to these types; the router uses them for
validation and OpenAPI documentation.
"""

from __future__ import annotations

from typing import Annotated, Literal

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
        "free-reminded",
    }
)

# Kinds that participate in lease-based concurrency control.
LEASED_KINDS: frozenset[str] = frozenset({"phase", "approved"})

# Valid contract statuses stored in sdd_contract.
ContractStatus = Literal["active", "archived", "superseded"]

# Valid contract route values.
ContractRoute = Literal["full", "sdd-fast", "add-skill"]

# SDD phases that a contract may belong to.
ContractPhase = Literal["intake", "spec", "design", "build", "qa", "ship"]


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
    lease_expires_at: str | None = None
    conflict: StateConflictInfo | None = None


class StateConflictInfo(BaseModel):
    """Conflict detail returned when a lease is held by another session."""

    owner: str
    lease_expires_at: str
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
    success_criteria: list[str] | None = Field(default=None, description="Success criteria list")
    body: str | None = Field(default=None, description="Contract body (markdown)")


class ContractPatchRequest(BaseModel):
    """Input to PATCH /contracts/{id} — in-place correction."""

    body: str | None = None
    domain_tags: list[str] | None = None
    scope_touches: list[str] | None = None
    scope_avoids: list[str] | None = None
    success_criteria: list[str] | None = None


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
    success_criteria: list[str] | None = None
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
    success_criteria: list[str] | None
    status: str
    supersedes: str | None
    created_at: str
    updated_at: str
    body: str | None


class ContractListResponse(BaseModel):
    """Response for GET /contracts."""

    contracts: list[ContractResponse]


class PhaseAdvanceResponse(BaseModel):
    """Response for POST /state/phase when a contract is included."""

    success: Literal[True] = True
    kind: str
    value: str
    owner: str | None = None
    lease_expires_at: str | None = None
    conflict: StateConflictInfo | None = None
    contract_id: str | None = None
