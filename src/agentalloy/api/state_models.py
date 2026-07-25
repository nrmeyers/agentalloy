"""Pydantic models for the state endpoint router.

Request and response shapes for POST /state/<kind> and GET /state/<kind>.
Handler implementations bind to these types; the router uses them for
validation and OpenAPI documentation.
"""

from __future__ import annotations

from datetime import datetime
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
