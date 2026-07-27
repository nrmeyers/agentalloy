"""SDD state and contract endpoints — GET/POST /state/* and /contracts routes.

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
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from agentalloy.api.state_models import (
    ALL_KINDS,
    ContractCreateRequest,
    ContractListResponse,
    ContractPatchRequest,
    ContractResponse,
    ContractSupersedeRequest,
    PhaseAdvanceRequest,
    PhaseAdvanceResponse,
    ResumeContractInfo,
    ResumeResponse,
    StateAllResponse,
    StateConflictInfo,
    StateReadResponse,
    StateWriteRequest,
    StateWriteResponse,
)
from agentalloy.storage.state_store import DuckDBStateStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/state", tags=["state"])
contract_router = APIRouter(prefix="/contracts", tags=["contracts"])


# ---------------------------------------------------------------------------
# Dependency providers — overridden during the app lifespan (or by tests).
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


def _write_result_to_response(
    result: Any,
) -> tuple[int, StateWriteResponse | StateConflictInfo]:
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


def _contract_row_to_response(row: dict[str, Any]) -> ContractResponse:
    """Convert a store contract dict to a ContractResponse model."""
    return ContractResponse(
        contract_id=row["contract_id"],
        phase=row["phase"],
        slug=row["slug"],
        work_item=row.get("work_item"),
        route=row.get("route"),
        domain_tags=row.get("domain_tags"),
        scope_touches=row.get("scope_touches"),
        scope_avoids=row.get("scope_avoids"),
        success_criteria=row.get("success_criteria"),
        status=row["status"],
        supersedes=row.get("supersedes"),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        body=row.get("body"),
    )


async def _trigger_compose_in_process(
    store: DuckDBStateStore,
    contract_id: str,
    request: Request,
) -> None:
    """Trigger compose in-process after a successful contract write.

    Retrieves the ComposeOrchestrator from app.state and composes using the
    stored contract's phase, domain_tags, and body.  Runs asynchronously so
    it does not block the HTTP response.
    """
    orchestrator = getattr(request.app.state, "compose_orchestrator", None)
    if orchestrator is None:
        logger.debug(
            "compose_orchestrator not available on app.state — skipping in-process compose"
        )
        return

    contract = store.get_contract(contract_id)
    if contract is None:
        logger.warning("Contract %s not found for compose trigger", contract_id)
        return

    from agentalloy.api.compose_models import ComposeRequest

    compose_req = ComposeRequest(
        task=contract.get("body") or contract.get("slug", ""),
        phase=contract["phase"],  # type: ignore[arg-type]
        contract_tags=contract.get("domain_tags"),
        requesting_agent="contract_write",
        legs="both",
    )

    try:
        await orchestrator.compose(compose_req)
    except Exception:
        logger.exception("In-process compose failed for contract %s", contract_id)


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
# POST /state/phase — with optional contract (transactional)
# ---------------------------------------------------------------------------


@router.post(
    "/phase",
    response_model=PhaseAdvanceResponse,
    responses={
        409: {"model": StateConflictInfo, "description": "Lease conflict"},
    },
    summary="Set the current SDD phase (with optional contract)",
)
async def write_phase(
    req: PhaseAdvanceRequest,
    request: Request,
    store: DuckDBStateStore = Depends(get_state_store),
) -> PhaseAdvanceResponse:
    """Advance the phase, optionally storing a contract in the same transaction.

    When ``req.contract`` is provided, both the phase write and the contract
    write commit together.  If the contract payload fails validation or the
    contract write fails, the phase advance is rolled back entirely.

    On success, compose is triggered in-process (the write *is* the trigger).
    """
    # Fast path: no contract — use the existing non-transactional path
    if req.contract is None:
        result = await asyncio.to_thread(store.write, "phase", req.value, owner=req.owner)
        http_status, response = _write_result_to_response(result)
        if http_status != 200:
            raise HTTPException(
                status_code=409,
                detail=response.model_dump(),  # type: ignore[union-attr]
            )
        return PhaseAdvanceResponse(
            kind=result.kind,
            value=result.value,
            owner=result.owner,
            lease_expires_at=(
                result.lease_expires_at.isoformat() if result.lease_expires_at else None
            ),
        )

    # Transactional path: phase + contract in one BEGIN/COMMIT
    contract_id: str | None = None
    try:
        with store.transaction() as tx:
            # Write the phase
            result = tx.write("phase", req.value, owner=req.owner)
            if result.conflict is not None:
                raise HTTPException(
                    status_code=409,
                    detail=StateConflictInfo(
                        owner=result.conflict.owner,
                        lease_expires_at=result.conflict.lease_expires_at.isoformat(),
                        message=result.conflict.message,
                    ).model_dump(),
                )

            # Write the contract
            contract = req.contract
            contract_id = tx.put_contract(
                contract.contract_id,
                phase=contract.phase,
                slug=contract.slug,
                work_item=contract.work_item,
                route=contract.route,
                domain_tags=contract.domain_tags,
                scope_touches=contract.scope_touches,
                scope_avoids=contract.scope_avoids,
                success_criteria=contract.success_criteria,
                body=contract.body,
            )
        # Transaction committed — trigger compose in-process
        asyncio.create_task(_trigger_compose_in_process(store, contract_id, request))
    except HTTPException:
        raise
    except Exception:
        # Transaction rolled back by context manager
        raise HTTPException(
            status_code=500,
            detail="Phase advance failed — transaction rolled back",
        ) from None

    return PhaseAdvanceResponse(
        kind=result.kind,
        value=result.value,
        owner=result.owner,
        lease_expires_at=(result.lease_expires_at.isoformat() if result.lease_expires_at else None),
        contract_id=contract_id,
    )


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
        raise HTTPException(
            status_code=409,
            detail=response.model_dump(),  # type: ignore[union-attr]
        )
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
        raise HTTPException(
            status_code=409,
            detail=response.model_dump(),  # type: ignore[union-attr]
        )
    return response  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# GET /state/resume — assembled cold-session bootstrap
# ---------------------------------------------------------------------------


@router.get(
    "/resume",
    response_model=ResumeResponse,
    summary="Assembled resume data for cold-session bootstrap",
)
async def read_resume(
    store: DuckDBStateStore = Depends(get_state_store),
) -> ResumeResponse:
    """Assemble phase, cursor'd work-item, owed artifacts, and governing decisions.

    This is a single server-side endpoint that replaces the client-side
    fan-out of four separate calls.  The shape matches what the proxy's
    orientation block sends to the agent.
    """
    phase = await asyncio.to_thread(store.read, "phase")
    cursor_value = await asyncio.to_thread(store.read, "cursor")

    cursor_contract: ResumeContractInfo | None = None
    owed_artifacts: list[str] | None = None
    governing_decisions: list[str] | None = None

    if cursor_value:
        # Extract contract_id from cursor value (format: "active/<phase>/<slug>.md")
        parts = cursor_value.strip().rsplit("/", 1)
        if len(parts) == 2:
            slug_file = parts[1]
            slug = slug_file.rsplit(".md", 1)[0] if slug_file.endswith(".md") else parts[1]

            # Find the contract by slug
            contracts = await asyncio.to_thread(store.list_contracts, slug=slug, status="active")
            if contracts:
                row = contracts[0]
                cursor_contract = ResumeContractInfo(
                    contract_id=row["contract_id"],
                    phase=row["phase"],
                    slug=row["slug"],
                    domain_tags=row.get("domain_tags"),
                    scope_touches=row.get("scope_touches"),
                    scope_avoids=row.get("scope_avoids"),
                    body=row.get("body"),
                )

    if phase:
        # Get owed artifacts from exit gates for the current phase
        try:
            from agentalloy.signals.skill_loader import exit_gates_for_phase

            gates = exit_gates_for_phase(phase) or {}
            artifacts: list[str] = []
            for _gate_name, gate_spec in gates.items():
                if isinstance(gate_spec, dict):
                    artifact = gate_spec.get("artifact") or gate_spec.get("artifact_contains")
                    if isinstance(artifact, str):
                        artifacts.append(artifact)
                    elif isinstance(artifact, list):
                        artifacts.extend(artifact)
            if artifacts:
                owed_artifacts = artifacts
        except Exception:
            pass

    return ResumeResponse(
        phase=phase,
        cursor_contract=cursor_contract,
        owed_artifacts=owed_artifacts,
        governing_decisions=governing_decisions,
    )


# ---------------------------------------------------------------------------
# POST /contracts — create
# ---------------------------------------------------------------------------


@contract_router.post(
    "",
    response_model=ContractResponse,
    responses={
        409: {"description": "Contract ID already exists"},
    },
    summary="Create a new contract",
)
async def create_contract(
    req: ContractCreateRequest,
    request: Request,
    store: DuckDBStateStore = Depends(get_state_store),
) -> ContractResponse:
    cid = await asyncio.to_thread(
        store.put_contract,
        req.contract_id,
        phase=req.phase,
        slug=req.slug,
        work_item=req.work_item,
        route=req.route,
        domain_tags=req.domain_tags,
        scope_touches=req.scope_touches,
        scope_avoids=req.scope_avoids,
        success_criteria=req.success_criteria,
        body=req.body,
    )

    # Trigger compose in-process — the write is the trigger
    asyncio.create_task(_trigger_compose_in_process(store, cid, request))

    row = store.get_contract(cid)
    if row is None:
        raise HTTPException(status_code=500, detail="Contract disappeared after write")
    return _contract_row_to_response(row)


# ---------------------------------------------------------------------------
# GET /contracts — list with filters
# ---------------------------------------------------------------------------


@contract_router.get(
    "",
    response_model=ContractListResponse,
    summary="List contracts with optional filters",
)
async def list_contracts(
    phase: str | None = Query(default=None, description="Filter by phase"),
    slug: str | None = Query(default=None, description="Filter by slug"),
    status: str | None = Query(default=None, description="Filter by status"),
    store: DuckDBStateStore = Depends(get_state_store),
) -> ContractListResponse:
    rows = await asyncio.to_thread(store.list_contracts, phase=phase, slug=slug, status=status)
    return ContractListResponse(contracts=[_contract_row_to_response(row) for row in rows])


# ---------------------------------------------------------------------------
# GET /contracts/{id} — read single
# ---------------------------------------------------------------------------


@contract_router.get(
    "/{contract_id}",
    response_model=ContractResponse,
    responses={
        404: {"description": "Contract not found"},
    },
    summary="Get a contract by ID",
)
async def get_contract(
    contract_id: str,
    store: DuckDBStateStore = Depends(get_state_store),
) -> ContractResponse:
    row = await asyncio.to_thread(store.get_contract, contract_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Contract {contract_id!r} not found")
    return _contract_row_to_response(row)


# ---------------------------------------------------------------------------
# PATCH /contracts/{id} — in-place correction
# ---------------------------------------------------------------------------


@contract_router.patch(
    "/{contract_id}",
    response_model=ContractResponse,
    responses={
        404: {"description": "Contract not found"},
    },
    summary="In-place correction of a contract",
)
async def patch_contract(
    contract_id: str,
    req: ContractPatchRequest,
    store: DuckDBStateStore = Depends(get_state_store),
) -> ContractResponse:
    updated = await asyncio.to_thread(
        store.update_contract,
        contract_id,
        body=req.body,
        domain_tags=req.domain_tags,
        scope_touches=req.scope_touches,
        scope_avoids=req.scope_avoids,
        success_criteria=req.success_criteria,
    )
    if not updated:
        raise HTTPException(status_code=404, detail=f"Contract {contract_id!r} not found")
    row = store.get_contract(contract_id)
    if row is None:
        raise HTTPException(status_code=500, detail="Contract disappeared after update")
    return _contract_row_to_response(row)


# ---------------------------------------------------------------------------
# POST /contracts/{id}/archive
# ---------------------------------------------------------------------------


@contract_router.post(
    "/{contract_id}/archive",
    response_model=ContractResponse,
    responses={
        404: {"description": "Contract not found or already archived"},
    },
    summary="Archive a contract",
)
async def archive_contract(
    contract_id: str,
    store: DuckDBStateStore = Depends(get_state_store),
) -> ContractResponse:
    archived = await asyncio.to_thread(store.archive_contract, contract_id)
    if not archived:
        raise HTTPException(
            status_code=404,
            detail=f"Contract {contract_id!r} not found or already archived",
        )
    row = store.get_contract(contract_id)
    if row is None:
        raise HTTPException(status_code=500, detail="Contract disappeared after archive")
    return _contract_row_to_response(row)


# ---------------------------------------------------------------------------
# POST /contracts/{id}/supersede
# ---------------------------------------------------------------------------


@contract_router.post(
    "/{contract_id}/supersede",
    response_model=ContractResponse,
    responses={
        404: {"description": "Contract not found"},
        400: {"description": "Cannot supersede contract in current status"},
    },
    summary="Supersede a contract with a new revision",
)
async def supersede_contract(
    contract_id: str,
    req: ContractSupersedeRequest,
    request: Request,
    store: DuckDBStateStore = Depends(get_state_store),
) -> ContractResponse:
    new_id = await asyncio.to_thread(
        store.supersede_contract,
        contract_id,
        new_contract_id=req.new_contract_id,
        phase=req.phase,
        slug=req.slug,
        work_item=req.work_item,
        route=req.route,
        domain_tags=req.domain_tags,
        scope_touches=req.scope_touches,
        scope_avoids=req.scope_avoids,
        success_criteria=req.success_criteria,
        body=req.body,
    )

    # Trigger compose in-process for the new contract
    asyncio.create_task(_trigger_compose_in_process(store, new_id, request))

    row = store.get_contract(new_id)
    if row is None:
        raise HTTPException(status_code=500, detail="New contract disappeared after write")
    return _contract_row_to_response(row)
