"""Proxy request telemetry.

Constructs a ``CompositionTrace`` for every proxy request (composed or
passthrough) and writes it to the vector store via ``record_composition_trace``.

Pattern mirrors ``signal.py``'s ``_write_telemetry`` — the signal CLI constructs
CompositionTrace directly and calls ``append_trace()``; the proxy path does the
same but receives a live VectorStore from the app context.

Public API
----------
write_proxy_trace
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Sequence

from agentalloy.storage.vector_store import CompositionTrace, VectorStore


def write_proxy_trace(
    vector_store: VectorStore,
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
    source_skill_ids: Sequence[str] | None = None,
    error_code: str | None = None,
    phase_gate_embed_failed: bool = False,
) -> None:
    """Write a CompositionTrace for a proxy request.

    Soft-fail: telemetry errors are swallowed so they never propagate to the
    caller of the proxy endpoint.

    Args:
        vector_store: Live VectorStore from the app context.
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
    """
    try:
        trace = CompositionTrace(
            trace_id=str(uuid.uuid4()),
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
            source_skill_ids=list(source_skill_ids) if source_skill_ids else [],
            error_code=error_code,
            phase_gate_embed_failed=phase_gate_embed_failed,
        )
        vector_store.record_composition_trace(trace)
    except Exception:  # noqa: BLE001 — soft-fail; telemetry never blocks the request
        pass
