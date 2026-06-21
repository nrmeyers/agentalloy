"""Signal layer integration for proxy requests.

Ties together the existing signal-layer components (pre-filter, gate
evaluation, phase transitions) so the proxy path can evaluate whether
a request should trigger skill composition.

Public API
----------
SignalResult
    evaluate_signal
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentalloy.api.proxy_models import ProxyRequest
from agentalloy.embed_provider import EmbedClient
from agentalloy.signals.classifier import check_transition_trigger
from agentalloy.signals.gates import INTAKE_PHASE, decide_transition
from agentalloy.signals.prefilter import PreFilterMatch
from agentalloy.signals.skill_loader import (  # type: ignore[reportPrivateUsage]
    _build_predicate_context,
    _intake_route_hint,
    _load_workflow_skill_for_phase,
    _read_lifecycle_mode,
    _read_phase,
    _write_phase_atomic,
)

logger = logging.getLogger(__name__)


@dataclass
class SignalResult:
    """Outcome of evaluating the signal layer for a proxy request."""

    should_compose: bool
    phase: str | None = None
    task: str | None = None
    domain_tags: list[str] | None = field(default_factory=lambda: list[str]())

    # Optional signal-layer metadata (for telemetry)
    pre_filter_matched: str | None = None
    gates_met: list[str] = field(default_factory=lambda: list[str]())
    gates_unmet: list[str] = field(default_factory=lambda: list[str]())
    qwen_calls: int = 0

    # Human-facing gate advisories (e.g. "intent fired but the exit artifact is
    # missing"). Surfaced to the agent alongside composed skills.
    advisories: list[str] = field(default_factory=lambda: list[str]())


def _extract_task_from_messages(request: ProxyRequest) -> str | None:
    """Extract the first user message text as the task prompt.

    ``ProxyMessage.content`` may be a plain string or a list of
    Anthropic-style content blocks. Flatten the block form to text so the
    return type stays ``str | None`` as annotated.
    """
    for msg in request.messages:
        if msg.role != "user" or not msg.content:
            continue
        if isinstance(msg.content, str):
            return msg.content
        parts = [block.get("text", "") for block in msg.content if block.get("type") == "text"]
        joined = "".join(parts)
        if joined:
            return joined
    return None


async def evaluate_signal(
    request: ProxyRequest,
    cwd: Path,
    embed_client: EmbedClient | None = None,
) -> SignalResult:
    """Evaluate the signal layer for an incoming proxy request.

    Flow:
    1. Read phase file (``.agentalloy/phase``)
    2. If no phase: return ``should_compose=False``
    3. Load workflow skill for the current phase
    4. Build PredicateContext from request data
    5. Run pre-filter (cheap keyword match)
    6. If pre-filter matched: run gate evaluation (may include semantic)
    7. If gates met: write phase transition atomically
    8. Return SignalResult indicating whether to compose

    Args:
        request: the incoming proxy request
        cwd: resolved working directory (project root)
        embed_client: optional client for semantic gate predicates

    Returns:
        SignalResult with composition decision and metadata
    """
    # 0. Per-repo lifecycle mode. Only `full` runs the phase lifecycle on the
    # proxy. `assist`/`off` defer entirely: the proxy has no phase-independent
    # injection path (all domain + system skills flow through this one compose),
    # so deferring the lifecycle means full passthrough here. The hook (Claude
    # Code) path offers the finer-grained `assist` that keeps system/domain
    # injection because those hooks fire independently of the phase. Guarding
    # before reading the phase means an assist/off repo that still has a stale
    # `.agentalloy/phase` (e.g. re-wired from full) is not composed for.
    mode = _read_lifecycle_mode(cwd)
    if mode != "full":
        logger.debug("composition deferred for %s: lifecycle_mode=%s", cwd, mode)
        return SignalResult(should_compose=False)

    # 1. Read phase file (sync, instant)
    phase = _read_phase(cwd)
    if not phase:
        return SignalResult(should_compose=False)

    task = _extract_task_from_messages(request)

    # 2. Load workflow skill for the phase (sync DB query — run in thread)
    skill = await asyncio.to_thread(_load_workflow_skill_for_phase, phase, cwd)
    if skill is None:
        return SignalResult(should_compose=False, phase=phase, task=task)

    signal_keywords: list[str] = skill.get("signal_keywords") or []
    exit_gates: dict[str, Any] = skill.get("exit_gates") or {}

    # 3. Build predicate context
    ctx = _build_predicate_context(
        project_root=cwd,
        phase=phase,
        prompt_text=task,
        # Proxy has no file/tool events — only prompt text
    )

    # 4. Transition trigger (reranker-primary, deterministic fallback floor).
    #    Intake is the entry phase: it must compose on the first prompt, before
    #    any signal exists, so it bypasses the trigger. Normal gating resumes
    #    once intake hands off to spec.
    match: PreFilterMatch | None
    if phase == INTAKE_PHASE:
        match = PreFilterMatch(name="intake_entry", detail="intake phase composes unconditionally")
    else:
        match = check_transition_trigger(signal_keywords, exit_gates, ctx, embed_client)
    if match is None:
        return SignalResult(
            should_compose=False,
            phase=phase,
            task=task,
        )

    # 5. Trigger matched — compose is warranted.
    # Run gate evaluation in a thread to avoid blocking the event loop.
    gates_result: SignalResult | None = None

    def _run_gates() -> None:
        nonlocal gates_result
        # Leaving intake branches on the contract route: fast → sdd-fast, else
        # the linear intake → spec.
        route_hint = _intake_route_hint(cwd) if phase == INTAKE_PHASE else None
        decision = decide_transition(
            current_phase=phase,
            gate_spec=exit_gates,
            ctx=ctx,
            lm_client=embed_client,
            next_phase_hint=route_hint,
        )
        # 6. Phase transition: write atomically if gates are met
        if decision.should_transition and decision.to_phase:
            try:
                _write_phase_atomic(cwd, decision.to_phase)
                logger.info("Phase transition: %s -> %s", phase, decision.to_phase)
            except OSError as e:
                logger.warning("Failed to write phase file: %s", e)

        gates_met = [g.gate_name for g in decision.gates_met]
        gates_unmet = [g.gate_name for g in decision.gates_unmet]

        gates_result = SignalResult(
            should_compose=True,
            phase=phase,
            task=task,
            domain_tags=skill.get("domain_tags"),
            pre_filter_matched=match.detail,
            gates_met=gates_met,
            gates_unmet=gates_unmet,
            qwen_calls=decision.qwen_calls,
            advisories=list(decision.advisories),
        )

    await asyncio.to_thread(_run_gates)

    if gates_result is not None:
        return gates_result

    # Fallback: pre-filter matched but gates didn't populate
    return SignalResult(
        should_compose=True,
        phase=phase,
        task=task,
        pre_filter_matched=match.detail,
    )
