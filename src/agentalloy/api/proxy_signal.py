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
    _read_announced,
    _read_composed,
    _read_cursor,
    _read_lifecycle_mode,
    _read_phase,
    _write_announced_atomic,
    _write_composed_atomic,
    _write_phase_atomic,
)

logger = logging.getLogger(__name__)

# Project roots we've already warned about being invisible to the proxy, so the
# WARNING fires at most once per repo per process instead of on every request.
_warned_missing_root: set[str] = set()


@dataclass
class SignalResult:
    """Outcome of evaluating the signal layer for a proxy request."""

    should_compose: bool
    phase: str | None = None
    task: str | None = None
    domain_tags: list[str] | None = field(default_factory=lambda: list[str]())

    # Tier 1 (phase-entry announce). True when this request is the first one in
    # *phase* (the phase changed since we last announced), so the workflow + system
    # prose for the phase is emitted exactly once. `workflow_prose` is that phase's
    # operating instructions (the workflow skill's raw_prose). See `_read_announced`.
    announce: bool = False
    workflow_prose: str | None = None

    # Tier 2 (per-work-item domain). `current_contract` is the absolute path to the
    # work-item contract whose domain skills should be composed (body → prompt,
    # domain_tags → BM25 steer). `announce_cursor` is True when the cursor changed
    # since we last composed it (phase entry, or an `agentalloy task next`), so the
    # task's domain block fires exactly once per work-item. See `_read_composed`.
    current_contract: str | None = None
    announce_cursor: bool = False

    # Optional signal-layer metadata (for telemetry)
    pre_filter_matched: str | None = None
    gates_met: list[str] = field(default_factory=lambda: list[str]())
    gates_unmet: list[str] = field(default_factory=lambda: list[str]())
    qwen_calls: int = 0

    # True when a semantic phase-gate (or the transition-trigger intent) hit an
    # embed failure this turn — the gate fell open to UNKNOWN and the transition
    # may have silently not fired. Surfaced so telemetry can distinguish an
    # infra-degraded gate from a legitimately-unmet one. See PredicateContext.
    phase_gate_embed_failed: bool = False

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


def _resolve_current_contract(cwd: Path, phase: str) -> tuple[str | None, Path | None]:
    """Resolve the current work-item contract for Tier 2 domain composition.

    Returns ``(contract_id, abs_path)`` where ``contract_id`` is the cursor's
    canonical value used for cadence (the contracts-relative posix path, e.g.
    ``build/01-cache.md``) and ``abs_path`` is the file to compose.

    Work-items for a phase live in ``.agentalloy/contracts/<phase>/`` and are
    authored by the *prior* phase (the cascade hand-off). Resolution:

    1. An explicit ``.agentalloy/cursor`` (set by ``agentalloy task next``) wins,
       when it resolves to a file under ``.agentalloy/contracts/``.
    2. Exactly one contract in ``contracts/<phase>/`` → that single work-item
       (the common single-item phase: spec/design/qa/ship).
    3. Two or more, no cursor → a fan-out phase (build): don't guess which task is
       current — stay silent until ``task next`` sets the cursor.
    4. None → ``(None, None)``; Tier 2 stays silent.
    """
    from agentalloy.contracts import list_contracts_for_phase

    contracts_root = (cwd / ".agentalloy" / "contracts").resolve()
    cursor = _read_cursor(cwd)
    if cursor:
        candidate = (contracts_root / cursor).resolve()
        # Containment guard: a stale/hostile cursor must not read outside the tree.
        if candidate.is_file() and candidate.is_relative_to(contracts_root):
            return candidate.relative_to(contracts_root).as_posix(), candidate
        logger.warning("cursor %r does not resolve to a contract file; using phase default", cursor)

    in_phase = list_contracts_for_phase(cwd, phase)
    if len(in_phase) != 1:
        # 0 → nothing to compose; ≥2 → fan-out, wait for the cursor.
        return None, None
    only = in_phase[0].resolve()
    return only.relative_to(contracts_root).as_posix(), only


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
        # A missing `.agentalloy/` here (lifecycle is active — we passed the
        # mode!="full" guard above) is the signature of a project root that
        # isn't visible to the proxy: in container mode the decoded host path
        # must be bind-mounted at this exact path. Warn once per repo so this
        # never fails silently as a plain passthrough.
        agentalloy_dir = cwd / ".agentalloy"
        if not agentalloy_dir.exists():
            key = str(cwd)
            if key not in _warned_missing_root:
                _warned_missing_root.add(key)
                logger.warning(
                    "lifecycle active but %s is not visible to the proxy — if AgentAlloy "
                    "runs in a container, the project root must be bind-mounted at this exact "
                    "path (see AGENTALLOY_PROJECTS_ROOT). Composition skipped for this repo.",
                    agentalloy_dir,
                )
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

    # 4. Announce cadence: a phase's orientation/domain block is emitted exactly
    #    once on entry. `.agentalloy/announced` records the last phase we
    #    announced; when it no longer matches the current phase (fresh wire, or a
    #    transition advanced us here) this is an entry turn and we announce. Turns
    #    in the middle of a phase do not re-announce — that every-turn re-compose
    #    (intake especially, which used to bypass the trigger and compose
    #    unconditionally) was the flood this replaces. Cadence lives in durable
    #    state, not in the request body: Claude Code never echoes an injected
    #    marker back, so the old marker-echo dedup was structurally dead.
    announce = _read_announced(cwd) != phase

    # 5. Transition trigger (reranker-primary intent, deterministic floor). Runs
    #    for every phase, including intake — there is no unconditional bypass. On
    #    a turn carrying no completion/approval signal the trigger does not fire,
    #    so an in-progress phase stays silent unless it is also an entry turn.
    match: PreFilterMatch | None = check_transition_trigger(
        signal_keywords, exit_gates, ctx, embed_client
    )

    # 6. Eval (only when the trigger fired): evaluate exit gates, transition the
    #    phase if met, and collect gate advisories. Runs in a thread so the
    #    file/embed work in decide_transition never blocks the event loop.
    advisories: list[str] = []
    gates_met: list[str] = []
    gates_unmet: list[str] = []
    qwen_calls = 0

    if match is not None:

        def _run_gates() -> None:
            nonlocal advisories, gates_met, gates_unmet, qwen_calls
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
            if decision.should_transition and decision.to_phase:
                try:
                    _write_phase_atomic(cwd, decision.to_phase)
                    logger.info("Phase transition: %s -> %s", phase, decision.to_phase)
                except OSError as e:
                    logger.warning("Failed to write phase file: %s", e)

            advisories = list(decision.advisories)
            gates_met = [g.gate_name for g in decision.gates_met]
            gates_unmet = [g.gate_name for g in decision.gates_unmet]
            qwen_calls = decision.qwen_calls

        await asyncio.to_thread(_run_gates)

    # Did any semantic gate / transition-trigger intent hit an embed failure this
    # turn? Read off the shared ctx (the trigger ran on this thread, the gates in
    # the worker thread — both mutate the same diagnostics sink, and to_thread
    # has already joined). Carried into telemetry so a silently-degraded gate is
    # queryable instead of only a WARNING line.
    phase_gate_embed_failed = ctx.embed_failed

    # 7. Tier 2 cadence: resolve the current work-item contract and decide whether
    #    its domain block fires. Tier 2 fires when the cursor changed since we last
    #    composed it — on phase entry (the incoming contract becomes current) or an
    #    `agentalloy task next`. Domain retrieval is keyed to the contract's task,
    #    NEVER the workflow's static process tags (which only ever emptied results).
    contract_id, contract_path = _resolve_current_contract(cwd, phase)
    announce_cursor = contract_id is not None and _read_composed(cwd) != contract_id

    # 8. Decide. Inject when this is a phase-entry turn (Tier 1), a new work-item
    #    turn (Tier 2), OR the eval produced advisories. None → quiet passthrough.
    if not (announce or announce_cursor or advisories):
        # A quiet turn. When a clean transition fired this turn (phase written, no
        # advisory), carry the gate metadata so telemetry still records the eval
        # even though nothing is injected — the new phase announces next turn.
        return SignalResult(
            should_compose=False,
            phase=phase,
            task=task,
            gates_met=gates_met,
            gates_unmet=gates_unmet,
            qwen_calls=qwen_calls,
            phase_gate_embed_failed=phase_gate_embed_failed,
        )

    # Record cadence state now so the next turn stays quiet. The signal layer owns
    # all `.agentalloy/` state transitions; marking at decision time is safe — if a
    # block later turns out empty, re-firing would inject nothing anyway.
    if announce:
        try:
            _write_announced_atomic(cwd, phase)
        except OSError as e:
            logger.warning("Failed to write announced file: %s", e)
    if announce_cursor and contract_id is not None:
        try:
            _write_composed_atomic(cwd, contract_id)
        except OSError as e:
            logger.warning("Failed to write composed file: %s", e)

    return SignalResult(
        should_compose=True,
        announce=announce,
        workflow_prose=skill.get("raw_prose") if announce else None,
        current_contract=str(contract_path) if announce_cursor and contract_path else None,
        announce_cursor=announce_cursor,
        phase=phase,
        task=task,
        pre_filter_matched=match.detail if match is not None else None,
        gates_met=gates_met,
        gates_unmet=gates_unmet,
        qwen_calls=qwen_calls,
        advisories=advisories,
        phase_gate_embed_failed=phase_gate_embed_failed,
    )
