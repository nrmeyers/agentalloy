"""Pydantic models for the compose endpoint.

Single source of truth for request and response shapes. Handler implementations
(NXS-768 onward) bind to these types; the 501 stub in ``compose_router`` uses
them to document the contract via OpenAPI.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from agentalloy.contracts import Contract

# intake/spec/design/build/qa/ship are the full SDD lifecycle; sdd-fast is the
# fast-lane route (one compressed pass) intake can branch to.
Phase = Literal["intake", "spec", "design", "build", "qa", "ship", "sdd-fast"]

# Phase-driven defaults (set 2026-04-25 from POC §15.7 findings).
# Short-form action phases get k=2 — Phase 1+2 data shows ~70% token reduction
# at parity quality. Long-form structured phases get k=4 — under-context
# at k=2 caused output rambling on T8 postmortem (truncated at max_tokens).
# Every value of the ``Phase`` Literal must appear here — ``resolved_k`` indexes
# this dict directly.
DEFAULT_K_BY_PHASE: dict[str, int] = {
    "build": 2,
    "ship": 2,
    "sdd-fast": 2,  # compressed action pass — short-form like build
    "qa": 4,  # safer default; long-form qa (postmortem) needs anchor context
    "spec": 4,
    "design": 4,
    "intake": 4,  # interview entry — wants anchor context for routing
}

# Recommended max_tokens hint surfaced in the response. Local-LLM callers
# tend to default to small caps and get truncated outputs (the T8 ramble
# on flat). These hints are sized to the typical fragment payload at the
# matching k. Keyed by every ``Phase`` value (compose indexes it directly).
DEFAULT_MAX_TOKENS_BY_PHASE: dict[str, int] = {
    "build": 2048,
    "ship": 2048,
    "sdd-fast": 2048,
    "qa": 4096,
    "spec": 4096,
    "design": 4096,
    "intake": 4096,
}

ErrorStage = Literal["retrieval", "assembly"]
ErrorCode = Literal[
    "dependency_unavailable",
    "store_unavailable",
    "embedding_failed",
    "embedding_model_unavailable",
]


class ComposeRequest(BaseModel):
    """Input to POST /compose."""

    task: Annotated[str, Field(min_length=1, description="Natural language task description")]
    phase: Phase = Field(description="SDD phase the task belongs to")
    domain_tags: list[str] | None = Field(
        default=None, description="Optional domain tag filter applied to domain fragments"
    )
    k: Annotated[
        int | None,
        Field(
            ge=1,
            le=50,
            description=(
                "Max domain candidates to assemble from. Omit to use the phase-driven "
                "default (k=2 for build/ship, k=4 for qa/spec/design/intake) — "
                "see DEFAULT_K_BY_PHASE."
            ),
        ),
    ] = None
    trace_id: str | None = Field(
        default=None,
        description="Caller-supplied correlation id. Logged alongside the server-generated composition_id.",
    )
    requesting_agent: str | None = Field(
        default=None,
        description=(
            "Origin of this compose (e.g. 'post_tool_use' for the contract hook). "
            "Recorded as the trace's correlation_id so hook-driven composes are "
            "distinguishable from direct /compose calls."
        ),
    )
    # Two-tier injection: which retrieval legs to assemble into ``output``.
    # "both" (default) = system + domain (direct /compose). "system" = Tier 1
    # phase-entry announce (system prose only; domain suppressed). "domain" =
    # Tier 2 per-work-item (domain only; system already announced in Tier 1).
    legs: Literal["both", "system", "domain"] = Field(
        default="both",
        description="Retrieval legs to assemble: both | system (Tier 1) | domain (Tier 2).",
    )
    # Contract integration (Phase 2)
    contract_path: str | None = Field(
        default=None,
        description="Absolute path to a contract markdown file. If provided, loads domain_tags from it.",
    )
    contract_tags: list[str] | None = Field(
        default=None,
        description="Explicit contract tags (bypasses contract_path loading; useful for tests).",
    )

    def resolved_k(self) -> int:
        """Server-side resolution: caller's k if provided, else phase default."""
        return self.k if self.k is not None else DEFAULT_K_BY_PHASE[self.phase]

    @property
    def resolved_contract_tags(self) -> list[str] | None:
        """Return contract domain_tags if available, else None.

        When loading from ``contract_path``, the path is run through the
        same containment guard the API endpoint uses — paths outside any
        ``.agentalloy/contracts/`` tree return ``None`` rather than reading
        arbitrary local files.
        """
        if self.contract_tags is not None:
            return self.contract_tags
        if self.contract_path is not None:
            from agentalloy.contracts import parse_contract, safe_contract_path

            safe_path, _ = safe_contract_path(self.contract_path)
            if safe_path is None:
                return None
            try:
                return parse_contract(safe_path).domain_tags
            except Exception:
                return None
        return None


def compose_request_from_contract(
    contract: Contract,
    *,
    legs: Literal["both", "system", "domain"] = "both",
    requesting_agent: str = "post_tool_use",
) -> ComposeRequest:
    """Map a parsed :class:`~agentalloy.contracts.Contract` to a ComposeRequest.

    Single source of truth for the contract→retrieval mapping, shared by the
    ``/compose/from-contract`` endpoint (``legs="both"``) and the proxy's Tier 2
    per-work-item injection (``legs="domain"``):

    - ``contract.body`` → ``task`` (the retrieval prompt; the design-authored task
      itself), falling back to ``task_slug`` for a body-less contract.
    - ``contract.domain_tags`` → ``contract_tags`` (a BM25 steer, **not** the hard
      ``domain_tags`` filter — that filter is what emptied retrieval when fed the
      workflow's static process tags).
    """
    return ComposeRequest(
        task=contract.body or contract.task_slug,
        phase=contract.phase,  # type: ignore[arg-type]
        contract_tags=contract.domain_tags,
        contract_path=str(contract.path),
        requesting_agent=requesting_agent,
        legs=legs,
    )


class LatencyBreakdown(BaseModel):
    retrieval_ms: int
    assembly_ms: int
    total_ms: int


class ComposeTelemetry(BaseModel):
    """Persisted-trace fields the orchestrator computes but the result body
    otherwise omits. Surfaced on the result so a caller that suppresses the
    orchestrator's internal trace write (the proxy, via ``record_trace=False``)
    can fold them into one consolidated row instead of losing them."""

    tokens_returned: int = 0
    tokens_flat_equivalent: int = 0
    workflow_skill_ids: list[str] = Field(default_factory=list)
    reranked: bool = False
    dense_leg_degraded: bool = False
    lm_assist_outcome: str = "disabled"
    lm_assist_model: str | None = None
    # Stage B selection detail (populated only on a HIT): kept (injected) vs
    # scored-but-dropped fragment ids, and per-fragment scores over the pool.
    lm_assist_kept_ids: list[str] = Field(default_factory=list)
    lm_assist_dropped_ids: list[str] = Field(default_factory=list)
    lm_assist_scores: dict[str, float] = Field(default_factory=dict)


class ComposedResult(BaseModel):
    """Successful composition — HTTP 200."""

    status: Literal["ok"] = "ok"
    result_type: Literal["composed"] = "composed"
    task: str
    phase: Phase
    output: str
    domain_fragments: list[str]
    source_skills: list[str]
    system_fragments: list[str]
    system_skills_applied: bool
    assembly_tier: int
    latency_ms: LatencyBreakdown
    recommended_max_tokens: int | None = Field(
        default=None,
        description=(
            "Hint for the caller's downstream LLM call. Sized to the assembled "
            "fragment payload so the model has enough budget to produce a complete "
            "response without truncating. Honoring it is optional."
        ),
    )
    dense_leg_degraded: bool = Field(
        default=False,
        description=(
            "True when the dense retrieval leg was skipped or fell back to BM25 "
            "(an embedding failure or an empty bounded query). Signals degraded "
            "retrieval quality for this response."
        ),
    )
    telemetry: ComposeTelemetry = Field(
        default_factory=ComposeTelemetry,
        description=(
            "Persisted-trace fields (tokens, Stage B detail, workflow skills) for "
            "callers that suppress the orchestrator's internal trace write."
        ),
    )


class EmptyResult(BaseModel):
    """No matching domain fragments — HTTP 200, not an error."""

    status: Literal["ok"] = "ok"
    result_type: Literal["empty"] = "empty"
    task: str
    phase: Phase
    output: Literal[""] = ""
    domain_fragments: list[str] = Field(default_factory=list)
    source_skills: list[str] = Field(default_factory=list)
    system_fragments: list[str]
    system_skills_applied: bool
    reason: Literal["no_domain_fragments_matched"] = "no_domain_fragments_matched"
    recommended_max_tokens: int | None = None
    dense_leg_degraded: bool = False
    telemetry: ComposeTelemetry = Field(default_factory=ComposeTelemetry)


class ErrorAvailable(BaseModel):
    """What the service did manage to retrieve before the stage failed."""

    domain_fragments: list[str] = Field(default_factory=list)
    system_fragments: list[str] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    """Dependency failure — HTTP 503. No partial composition in the body."""

    status: Literal["error"] = "error"
    stage: ErrorStage
    code: ErrorCode
    message: str
    available: ErrorAvailable | None = None


ComposeResponse = ComposedResult | EmptyResult
