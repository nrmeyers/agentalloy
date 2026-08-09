"""Direct retrieve endpoints — GET /retrieve/{skill_id} and POST /retrieve."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from agentalloy.api.compose_models import ErrorResponse
from agentalloy.api.retrieve_models import (
    RetrieveByIdResponse,
    RetrieveQueryRequest,
    RetrieveQueryResponse,
)
from agentalloy.orchestration.retrieve import RetrieveOrchestrator

router = APIRouter()


def get_retrieve_orchestrator() -> RetrieveOrchestrator:
    raise RuntimeError(
        "get_retrieve_orchestrator must be bound during app lifespan; no default available",
    )


@router.get(
    "/retrieve/{skill_id}",
    response_model=RetrieveByIdResponse,
    responses={
        404: {"description": "No active skill with the given id"},
        503: {"model": ErrorResponse, "description": "Runtime store unavailable"},
    },
    summary="Retrieve active skill by id",
)
async def retrieve_by_id(
    skill_id: str,
    orchestrator: RetrieveOrchestrator = Depends(get_retrieve_orchestrator),
) -> RetrieveByIdResponse:
    result = await orchestrator.by_id(skill_id)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"skill {skill_id!r} not found or has no active version",
        )
    return result


@router.post(
    "/retrieve",
    response_model=RetrieveQueryResponse,
    responses={503: {"model": ErrorResponse}},
    summary="Semantic retrieve — returns active skill versions without assembly",
)
async def retrieve_query(
    req: RetrieveQueryRequest,
    orchestrator: RetrieveOrchestrator = Depends(get_retrieve_orchestrator),
) -> RetrieveQueryResponse:
    return await orchestrator.by_query(
        task=req.task,
        phase=req.phase,
        domain_tags=req.domain_tags,
        k=req.resolved_k(),
    )
