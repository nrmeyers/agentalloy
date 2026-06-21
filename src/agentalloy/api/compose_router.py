"""Compose endpoint router — real handler wired to ComposeOrchestrator."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from agentalloy.api.compose_models import (
    ComposedResult,
    ComposeRequest,
    EmptyResult,
    ErrorResponse,
)
from agentalloy.orchestration.compose import ComposeOrchestrator

router = APIRouter()


# Dependency provider — overridden in tests via app.dependency_overrides[].
def get_orchestrator() -> ComposeOrchestrator:
    raise RuntimeError("get_orchestrator must be bound during app lifespan; no default available")


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
    contract_path: str


@router.post(
    "/compose/from-contract",
    response_model=ComposedResult | EmptyResult,
    responses={
        400: {"model": ErrorResponse, "description": "Contract malformed or invalid"},
        503: {"model": ErrorResponse, "description": "Retrieval or assembly stage failure"},
    },
    summary="Compose using a contract file",
    description=(
        "Reads phase and domain_tags from a contract file, uses the contract body "
        "as the task description, and runs the standard compose pipeline."
    ),
)
async def compose_from_contract(
    req: FromContractRequest,
    orchestrator: ComposeOrchestrator = Depends(get_orchestrator),
) -> ComposedResult | EmptyResult:
    from agentalloy.contracts import (
        ContractMalformed,
        parse_contract,
        safe_contract_path,
        validate_contract,
    )

    # Containment guard: the supplied path must resolve to a file under
    # some project's .agentalloy/contracts/ tree. Rejects path traversal,
    # symlinks pointing outside, and arbitrary local-file reads.
    safe_path, project_root = safe_contract_path(req.contract_path)
    if safe_path is None or project_root is None:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "contract_path_unsafe",
                "issues": [
                    "contract_path must be an absolute path to a file under "
                    "a project's .agentalloy/contracts/ directory"
                ],
            },
        )

    try:
        contract = parse_contract(safe_path)
    except ContractMalformed as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "contract_malformed", "issues": [str(exc)]},
        ) from exc

    issues = validate_contract(contract, project_root)
    if issues:
        raise HTTPException(
            status_code=400,
            detail={"error": "contract_invalid", "issues": issues},
        )

    compose_req = ComposeRequest(
        task=contract.body or contract.task_slug,
        phase=contract.phase,  # type: ignore[arg-type]
        contract_tags=contract.domain_tags,
        contract_path=str(safe_path),
        # Tag the origin so PostToolUse-driven composes are distinguishable in
        # telemetry from direct /compose calls (lands in trace.correlation_id).
        requesting_agent="post_tool_use",
    )
    return await orchestrator.compose(compose_req)
