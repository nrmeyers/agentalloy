"""Local-agent HTTP surface (M1d) — POST /local-agent/ask, GET /local-agent/health.

Mounted only when ``LOCAL_AGENT=on`` (see ``app.py``); when the module is off
the endpoints 404 rather than 503. Request/response shapes per
``docs/local-agent-design.md``:

POST /local-agent/ask
    {"question": str, "repo"?: slug, "repo_root"?: path, "phase"?: phase}
    200 → {"answer", "steps": [{step, action, args, validation,
            result_chars, stage_latencies}], "degraded",
          "degrade_reason", "stop_reason", "model_tag", "total_ms"}
    400 → unknown phase or a repo_root that is not a directory
    503 → {"code": "local_agent_unavailable", "stage", "reason"} — an LM
          stage was unavailable. The process-wide cooldown latch stays
          tripped, so later asks return the same structured 503 without any
          HTTP attempt until the process restarts (the v1 contract).

GET /local-agent/health
    Reports the process-wide endpoint latch. Deliberately a cheap read — it
    does not probe the LM over HTTP (a dead local model would only slow
    every healthcheck, and the latch already captures the process state).

One trace row is written per finished (including degraded) ask — see
``local_agent/telemetry.py``. The 503 path is not traced: the loop never
completed, and the reason is in the response body and the logs.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from agentalloy.api.state_router import (
    _repo_key_for,
    default_repo_root,
    get_state_store,
)
from agentalloy.local_agent.client import ClientStageError, OpenAICompatClient, endpoint_down_reason
from agentalloy.local_agent.config import get_config
from agentalloy.local_agent.executors import Executors
from agentalloy.local_agent.loop import LocalAgentLoop, LoopResult
from agentalloy.local_agent.protocol import PHASES
from agentalloy.local_agent.validate import Validator
from agentalloy.storage.state_store import DuckDBStateStore

logger = logging.getLogger(__name__)

router = APIRouter(tags=["local-agent"])


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=8000, description="The question to answer")
    repo: str | None = Field(
        default=None,
        description="Repo slug scoping code-index actions (the worktree-collapsing slug).",
    )
    repo_root: str | None = Field(
        default=None,
        description=(
            "Absolute path to the repository root. Omitted means the repo the "
            "service was deployed against (AGENTALLOY_PROJECT_DIR, else the "
            "process cwd)."
        ),
    )
    phase: str | None = Field(
        default=None,
        description=f"Lifecycle phase scoping the lookup (one of: {', '.join(PHASES)}).",
    )


class AskStep(BaseModel):
    step: int
    action: str
    args: dict[str, Any]
    validation: str | None = None
    result_chars: int | None = None
    stage_latencies: dict[str, int] = Field(default_factory=dict)


class AskResponse(BaseModel):
    answer: str
    steps: list[AskStep]
    degraded: bool
    degrade_reason: str | None = None
    stop_reason: str
    model_tag: str
    total_ms: int


def _resolve_root(repo_root: str | None) -> Path:
    if repo_root is None:
        return default_repo_root()
    root = Path(repo_root).expanduser()
    if not root.is_dir():
        raise HTTPException(status_code=400, detail=f"repo_root {repo_root!r} is not a directory")
    return root


def _trace_repo(root: Path) -> str | None:
    """Trace attribution — the same rule the executors use (None = default repo)."""
    if root == Path.cwd():
        return None
    return _repo_key_for(str(root))


def _record_trace(
    request: Request,
    result: LoopResult,
    *,
    phase: str | None,
    repo: str | None,
    question: str,
) -> None:
    writer = getattr(request.app.state, "local_agent_trace_writer", None)
    if writer is not None:
        writer.record(result=result, question=question, phase=phase, repo=repo)


@router.post("/local-agent/ask", response_model=AskResponse)
async def local_agent_ask(
    body: AskRequest,
    request: Request,
    state_store: DuckDBStateStore = Depends(get_state_store),
) -> AskResponse:
    config = get_config()
    if body.phase is not None and body.phase not in PHASES:
        raise HTTPException(
            status_code=400,
            detail=f"unknown phase {body.phase!r}; expected one of: {', '.join(PHASES)}",
        )
    root = _resolve_root(body.repo_root)
    app_state = request.app.state
    code_index_state = getattr(app_state, "code_index_state", None)
    loop = LocalAgentLoop(
        OpenAICompatClient(config),
        config=config,
        validator=Validator(
            code_index_state=code_index_state,
            state_store=state_store,
            repo_slug=body.repo,
            repo_root=root,
        ),
        executors=Executors(
            code_index_state=code_index_state,
            state_store=state_store,
            telemetry_querier=getattr(app_state, "telemetry_querier", None),
            compose_orchestrator=getattr(app_state, "compose_orchestrator", None),
            repo_root=root,
        ),
    )
    try:
        result = await loop.run(body.question, repo=body.repo, phase=body.phase)
    except ClientStageError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "local_agent_unavailable",
                "stage": exc.stage,
                "reason": exc.cause,
            },
        ) from exc
    _record_trace(
        request,
        result,
        phase=body.phase,
        repo=_trace_repo(root) if body.repo is None else body.repo,
        question=body.question,
    )
    return AskResponse(
        answer=result.answer,
        steps=[
            AskStep(
                step=s.step,
                action=s.action,
                args=s.args,
                validation=s.validation,
                result_chars=s.result_chars,
                stage_latencies=s.stage_latencies_ms,
            )
            for s in result.steps
        ],
        degraded=result.degraded,
        degrade_reason=result.degrade_reason,
        stop_reason=result.stop_reason,
        model_tag=result.model_tag,
        total_ms=result.total_ms,
    )


@router.get("/local-agent/health")
async def local_agent_health() -> dict[str, Any]:
    config = get_config()
    reason = endpoint_down_reason()
    if reason is not None:
        return {"status": "degraded", "model": config.model, "url": config.url, "reason": reason}
    return {"status": "ok", "model": config.model, "url": config.url}
