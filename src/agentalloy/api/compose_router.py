"""Compose endpoint router — real handler wired to ComposeOrchestrator."""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, model_validator

from agentalloy.api.compose_models import (
    ComposedResult,
    ComposeRequest,
    EmptyResult,
    ErrorResponse,
    compose_request_from_contract,
)
from agentalloy.contracts import contract_from_row
from agentalloy.orchestration.compose import ComposeOrchestrator
from agentalloy.storage.state_store import DuckDBStateStore

router = APIRouter()

# Pattern to detect path-shaped inputs (absolute/relative paths, or paths with
# file extensions). These should be rejected — the endpoint takes contract_id,
# not a filesystem path.
_PATH_PATTERN = re.compile(r"(^/|^\./|^\.\./|\.md$|/.*\.md)")


# Dependency provider — overridden in tests via app.dependency_overrides[].
def get_orchestrator() -> ComposeOrchestrator:
    raise RuntimeError("get_orchestrator must be bound during app lifespan; no default available")


def get_state_store() -> DuckDBStateStore:
    """Return the lifespan-scoped StateStore.

    In-process callers inside the service hold the StateStore directly and do
    NOT make an HTTP call. Only out-of-process callers (CLI, web UI) go over
    HTTP. See approach.md §"A2 vs E2 is not a contradiction".
    """
    raise RuntimeError("get_state_store must be bound during app lifespan; no default available")


@router.post(
    "/compose",
    response_model=ComposedResult | EmptyResult,
    responses={
        503: {"model": ErrorResponse, "description": "Retrieval or assembly stage failure"},
    },
    summary="Compose task-specific guidance",
    description=(
        "Returns assembled guidance from active domain fragments plus applicable "
        "system-skill fragments. System-skill inclusion is stubbed in M1 and lands "
        "with NXS-771/NXS-772 in M2."
    ),
)
async def compose(
    req: ComposeRequest,
    orchestrator: ComposeOrchestrator = Depends(get_orchestrator),
) -> ComposedResult | EmptyResult:
    return await orchestrator.compose(req)


@router.post(
    "/compose/text",
    response_class=PlainTextResponse,
    summary="Compose task-specific guidance as plain text",
    description="Returns only the assembled skill text — no JSON wrapper. Intended for agent curl calls.",
)
async def compose_text(
    req: ComposeRequest,
    orchestrator: ComposeOrchestrator = Depends(get_orchestrator),
) -> PlainTextResponse:
    result = await orchestrator.compose(req)
    return PlainTextResponse(content=result.output)


class FromContractRequest(BaseModel):
    """Request body for /compose/from-contract.

    Takes a contract_id (store key) — not a filesystem path. Path-shaped inputs
    are rejected to eliminate the traversal attack surface that safe_contract_path
    previously defended.
    """

    contract_id: str

    @model_validator(mode="before")
    @classmethod
    def _reject_path_input(cls, data: dict[str, Any]) -> dict[str, Any]:
        """Validate and reject path-shaped inputs."""
        raw_id = data.get("contract_id")
        if raw_id is None:
            raise ValueError("contract_id is required")
        raw_id = str(raw_id)
        if _PATH_PATTERN.search(raw_id):
            raise ValueError(f"contract_id must be a store identifier, not a path: {raw_id!r}")
        if not raw_id.strip():
            raise ValueError("contract_id must be a non-empty string")
        return {"contract_id": raw_id.strip()}


@router.post(
    "/compose/from-contract",
    response_model=ComposedResult | EmptyResult,
    responses={
        400: {"model": ErrorResponse, "description": "Contract not found or invalid"},
        503: {"model": ErrorResponse, "description": "Retrieval or assembly stage failure"},
    },
    summary="Compose using a contract from the store",
    description=(
        "Looks up a contract by contract_id in the state store, uses its body "
        "as the task description, and runs the standard compose pipeline."
    ),
)
async def compose_from_contract(
    req: FromContractRequest,
    orchestrator: ComposeOrchestrator = Depends(get_orchestrator),
    store: DuckDBStateStore = Depends(get_state_store),
) -> ComposedResult | EmptyResult:
    # Load contract from the store (in-process, no HTTP hop)
    row = store.get_contract(req.contract_id)
    if row is None:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "contract_not_found",
                "issues": [f"No contract with id {req.contract_id!r} in the store"],
            },
        )

    try:
        contract = contract_from_row(row)
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "contract_malformed", "issues": [str(exc)]},
        ) from exc

    # Shared contract→request mapping (see compose_models). The endpoint composes
    # both legs; the proxy's Tier 2 path uses the same helper with legs="domain".
    # Origin tag "post_tool_use" lands in trace.correlation_id to distinguish
    # contract-driven composes from direct /compose calls.
    compose_req = compose_request_from_contract(contract, legs="both")
    return await orchestrator.compose(compose_req)
