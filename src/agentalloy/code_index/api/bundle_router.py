"""Context-bundle endpoint (``POST /code/context-bundle``)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from agentalloy.code_index.api.deps import require_indexed_repo
from agentalloy.code_index.api.state import CodeIndexState, get_code_index_state
from agentalloy.code_index.retrieval.bundle import Bundle, build_bundle

router = APIRouter()


class BundleRequest(BaseModel):
    """POST /code/context-bundle body."""

    repo: str = Field(description="Indexed repo slug")
    task: str = Field(min_length=1, description="Natural-language task description")
    budget_chars: int = Field(
        default=24000, ge=500, le=500_000, description="Source-excerpt character budget"
    )


@router.post(
    "/context-bundle",
    response_model=Bundle,
    summary="Assemble a budgeted code context for a task",
)
async def context_bundle(
    req: BundleRequest,
    state: CodeIndexState = Depends(get_code_index_state),
) -> Bundle:
    require_indexed_repo(state, req.repo)
    return await build_bundle(state, req.repo, req.task, budget_chars=req.budget_chars)
