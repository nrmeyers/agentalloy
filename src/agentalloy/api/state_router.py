"""SDD state endpoints — GET/POST /state/* routes.

Exposes the DuckDB-backed SDD state store over HTTP so out-of-process callers
(CLI subcommands, web UI) can read and mutate phase, cursor, and approval
state through the service.

In-process callers inside the service (compose, proxy_signal, signals) hold
the StateStore directly and do **not** make an HTTP call.  Only out-of-process
callers go over HTTP.  This split avoids a network hop on the hot compose path
while keeping a single source of truth for state mutations.

The store is opened once during the app lifespan and injected into routes via
the :func:`get_state_store` dependency.  Routes never open the database
themselves.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from agentalloy.api.state_models import (
    ALL_KINDS,
    StateAllResponse,
    StateConflictInfo,
    StateReadResponse,
    StateWriteRequest,
    StateWriteResponse,
)
from agentalloy.storage.state_store import DuckDBStateStore

router = APIRouter(prefix="/state", tags=["state"])


# ---------------------------------------------------------------------------
# Dependency provider — overridden during the app lifespan (or by tests).
# ---------------------------------------------------------------------------


def get_state_store() -> DuckDBStateStore:
    """Return the lifespan-scoped StateStore.

    Raises :class:`RuntimeError` if called before the lifespan has wired the
    store (e.g. a bare router without a running app).
    """
    raise RuntimeError("get_state_store must be bound during app lifespan")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_result_to_response(result: Any) -> tuple[int, StateWriteResponse | StateConflictInfo]:
    """Convert a StateWriteResult into an HTTP status + response model."""
    conflict = result.conflict
    if conflict is not None:
        return 409, StateConflictInfo(
            owner=conflict.owner,
            lease_expires_at=conflict.lease_expires_at.isoformat(),
            message=conflict.message,
        )
    return 200, StateWriteResponse(
        kind=result.kind,
        value=result.value,
        owner=result.owner,
        lease_expires_at=(result.lease_expires_at.isoformat() if result.lease_expires_at else None),
    )


# ---------------------------------------------------------------------------
# GET /state — all kinds
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=StateAllResponse,
    summary="Read all state kinds for the resolved repo",
)
async def read_all_state(
    store: DuckDBStateStore = Depends(get_state_store),
) -> StateAllResponse:
    state: dict[str, str] = {}
    for kind in sorted(ALL_KINDS):
        value = await asyncio.to_thread(store.read, kind)
        if value is not None:
            state[kind] = value
    return StateAllResponse(state=state)


# ---------------------------------------------------------------------------
# GET /state/{kind} — single kind
# ---------------------------------------------------------------------------


@router.get(
    "/{kind}",
    response_model=StateReadResponse,
    responses={
        404: {"description": "Kind not recognized"},
    },
    summary="Read a single state kind",
)
async def read_state(
    kind: str,
    store: DuckDBStateStore = Depends(get_state_store),
) -> StateReadResponse:
    if kind not in ALL_KINDS:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown state kind {kind!r}; expected one of {sorted(ALL_KINDS)}",
        )
    value = await asyncio.to_thread(store.read, kind)
    return StateReadResponse(kind=kind, value=value)


# ---------------------------------------------------------------------------
# POST /state/phase
# ---------------------------------------------------------------------------


@router.post(
    "/phase",
    response_model=StateWriteResponse | StateConflictInfo,
    responses={
        409: {"model": StateConflictInfo, "description": "Lease conflict"},
    },
    summary="Set the current SDD phase",
)
async def write_phase(
    req: StateWriteRequest,
    store: DuckDBStateStore = Depends(get_state_store),
) -> StateWriteResponse | StateConflictInfo:
    result = await asyncio.to_thread(store.write, "phase", req.value, owner=req.owner)
    http_status, response = _write_result_to_response(result)
    if http_status != 200:
        raise HTTPException(status_code=409, detail=response.model_dump())  # type: ignore[union-attr]
    return response  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# POST /state/cursor
# ---------------------------------------------------------------------------


@router.post(
    "/cursor",
    response_model=StateWriteResponse | StateConflictInfo,
    responses={
        409: {"model": StateConflictInfo, "description": "Lease conflict"},
    },
    summary="Set the work-item cursor",
)
async def write_cursor(
    req: StateWriteRequest,
    store: DuckDBStateStore = Depends(get_state_store),
) -> StateWriteResponse | StateConflictInfo:
    result = await asyncio.to_thread(store.write, "cursor", req.value, owner=req.owner)
    http_status, response = _write_result_to_response(result)
    if http_status != 200:
        raise HTTPException(status_code=409, detail=response.model_dump())  # type: ignore[union-attr]
    return response  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# POST /state/approve
# ---------------------------------------------------------------------------


@router.post(
    "/approve",
    response_model=StateWriteResponse | StateConflictInfo,
    responses={
        409: {"model": StateConflictInfo, "description": "Lease conflict"},
    },
    summary="Record an approval for a phase",
)
async def write_approve(
    req: StateWriteRequest,
    store: DuckDBStateStore = Depends(get_state_store),
) -> StateWriteResponse | StateConflictInfo:
    result = await asyncio.to_thread(store.write, "approved", req.value, owner=req.owner)
    http_status, response = _write_result_to_response(result)
    if http_status != 200:
        raise HTTPException(status_code=409, detail=response.model_dump())  # type: ignore[union-attr]
    return response  # type: ignore[return-value]
