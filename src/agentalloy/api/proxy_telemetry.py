"""Proxy request telemetry.

Constructs one consolidated ``CompositionTrace`` for every proxy request
(composed or passthrough) — folding in both compose tiers' skill/fragment
provenance and token counts — and writes it via ``record_composition_trace``
on the live TelemetryStore (telemetry.duck) from the app context.

Public API
----------
write_proxy_trace
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Sequence

from agentalloy.storage.protocols import CompositionTrace, TelemetryStore


def write_proxy_trace(
    vector_store: TelemetryStore,
    *,
    phase: str,
    task_prompt: str,
    status: str,
    event_type: str = "proxy_request",
    pre_filter_matched: str | None = None,
    gates_met: Sequence[str] | None = None,
    gates_unmet: Sequence[str] | None = None,
    qwen_calls: int = 0,
    total_latency_ms: int | None = None,
    retrieval_latency_ms: int | None = None,
    source_skill_ids: Sequence[str] | None = None,
    system_skill_ids: Sequence[str] | None = None,
    workflow_skill_ids: Sequence[str] | None = None,
    selected_fragment_ids: Sequence[str] | None = None,
    tokens_returned: int = 0,
    tokens_flat_equivalent: int = 0,
    reranked: bool = False,
    lm_assist_outcome: str = "disabled",
    lm_assist_model: str | None = None,
    lm_assist_kept_ids: Sequence[str] | None = None,
    lm_assist_dropped_ids: Sequence[str] | None = None,
    lm_assist_scores: str | None = None,
    dense_leg_degraded: bool = False,
    error_code: str | None = None,
    phase_gate_embed_failed: bool = False,
    repo: str | None = None,
    session_key: str | None = None,
    session_source: str | None = None,
    category: str | None = None,
) -> None:
    """Write a CompositionTrace for a proxy request.

    Soft-fail: telemetry errors are swallowed so they never propagate to the
    caller of the proxy endpoint.

    Args:
        vector_store: Live TelemetryStore (telemetry.duck) from the app context.
        phase: Current phase string (or "unspecified").
        task_prompt: First user message content (truncated to 500 chars).
        status: ``"proxy_composed"`` or ``"proxy_passthrough"``.
        event_type: Defaults to ``"proxy_request"``.
        pre_filter_matched: Pre-filter match name, or None.
        gates_met: Names of gates that passed.
        gates_unmet: Names of gates that did not pass.
        qwen_calls: Number of LLM calls made during gate evaluation.
        total_latency_ms: Total proxy request latency in milliseconds.
        source_skill_ids: Skill IDs injected into the system message.
        error_code: Error message if the request failed.
        phase_gate_embed_failed: True when a semantic phase-gate / transition-trigger
            embed call failed this turn (gate fell open to UNKNOWN, transition may
            not have fired).
        repo: Resolved project root (the request's cwd) this trace belongs to, so
            telemetry can be scoped per-repo. None leaves the row unattributed.
        category: Mode tag reusing the existing free-text ``category`` column —
            ``"free-flow"`` for a request handled in free-flow mode, else None
            (workflow mode). Lets free→contract conversion be measured later.
    """
    try:
        trace = CompositionTrace(
            trace_id=str(uuid.uuid4()),
            category=category,
            request_ts=int(time.time() * 1000),
            phase=phase,
            task_prompt=task_prompt[:500],
            status=status,
            event_type=event_type,
            pre_filter_matched=pre_filter_matched,
            gates_met=list(gates_met) if gates_met else [],
            gates_unmet=list(gates_unmet) if gates_unmet else [],
            qwen_calls=qwen_calls,
            total_latency_ms=total_latency_ms,
            retrieval_latency_ms=retrieval_latency_ms,
            source_skill_ids=list(source_skill_ids) if source_skill_ids else [],
            system_skill_ids=list(system_skill_ids) if system_skill_ids else [],
            workflow_skill_ids=list(workflow_skill_ids) if workflow_skill_ids else [],
            selected_fragment_ids=list(selected_fragment_ids) if selected_fragment_ids else [],
            tokens_returned=tokens_returned,
            tokens_flat_equivalent=tokens_flat_equivalent,
            reranked=reranked,
            lm_assist_outcome=lm_assist_outcome,
            lm_assist_model=lm_assist_model,
            lm_assist_kept_ids=list(lm_assist_kept_ids) if lm_assist_kept_ids else [],
            lm_assist_dropped_ids=list(lm_assist_dropped_ids) if lm_assist_dropped_ids else [],
            lm_assist_scores=lm_assist_scores,
            dense_leg_degraded=dense_leg_degraded,
            error_code=error_code,
            phase_gate_embed_failed=phase_gate_embed_failed,
            repo=repo,
            session_key=session_key,
            session_source=session_source,
        )
        vector_store.record_composition_trace(trace)
    except Exception:  # noqa: BLE001 — soft-fail; telemetry never blocks the request
        pass
