"""Hook telemetry — record every prompt and every skill pull from the hook router.

The proxy path records each request via ``write_proxy_trace``; the hook router
historically recorded only the orchestrator-backed composes (PostToolUse, and the
LM-assist compose), leaving SessionStart intake, the UserPromptSubmit decision
(including *no-compose* and cache hits), and PreToolUse system-skill selection
invisible to ``composition_traces``. ``write_hook_trace`` closes that gap.

It writes through the SAME live ``VectorStore`` the proxy uses (never a fresh
connection — uvicorn holds the single DuckDB read-write lock), and is soft-fail:
a telemetry error never blocks a hook. Columns are *reused*, not added —
``event_type`` carries the hook event and ``status`` the decision, both distinct
from ``'compose'`` so the token-savings aggregation (``status = 'compose'``) is
unaffected.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Sequence

from agentalloy.storage.vector_store import CompositionTrace, VectorStore

# Hook event name -> ``event_type`` column value. Each is a recognized
# composition_traces event_type so existing trace consumers stay valid.
_EVENT_TYPE = {
    "session_start": "session_intake",
    "user_prompt_submit": "prompt_submit",
    "pre_tool_use": "system_skill_applied",
}


def write_hook_trace(
    vector_store: VectorStore | None,
    *,
    hook_event: str,
    phase: str,
    status: str,
    task_prompt: str = "",
    workflow_skill_ids: Sequence[str] | None = None,
    system_skill_ids: Sequence[str] | None = None,
    selected_fragment_ids: Sequence[str] | None = None,
    correlation_id: str | None = None,
    total_latency_ms: int | None = None,
) -> None:
    """Record one hook event to ``composition_traces`` (soft-fail).

    Args:
        vector_store: The live VectorStore from ``app.state``; ``None`` (runtime
            not loaded) is a no-op.
        hook_event: ``"session_start"`` | ``"user_prompt_submit"`` |
            ``"pre_tool_use"`` — mapped to the ``event_type`` column.
        phase: Effective phase (``"unspecified"`` substituted when empty, since
            the column is NOT NULL).
        status: Decision for this event — e.g. ``"composed"`` / ``"no_compose"``
            (prompt), ``"system_skill"`` (pre-tool-use), ``"intake"`` (session).
            Kept distinct from ``"compose"`` so savings stay accurate.
        task_prompt: The user prompt (prompt-submit); empty for the others.
        workflow_skill_ids / system_skill_ids / selected_fragment_ids: the skills
            actually injected by this event.
        correlation_id: secondary descriptor without a new column — the cache
            status for prompt-submit (``"fresh"``/``"stale"``/``"cached"``) or the
            tool name for pre-tool-use.
        total_latency_ms: handler latency.
    """
    if vector_store is None:
        return
    try:
        trace = CompositionTrace(
            trace_id=str(uuid.uuid4()),
            request_ts=int(time.time() * 1000),
            phase=phase or "unspecified",
            task_prompt=task_prompt[:500],
            status=status,
            event_type=_EVENT_TYPE.get(hook_event, "phase_eval"),
            correlation_id=correlation_id,
            workflow_skill_ids=list(workflow_skill_ids) if workflow_skill_ids else [],
            system_skill_ids=list(system_skill_ids) if system_skill_ids else [],
            selected_fragment_ids=list(selected_fragment_ids) if selected_fragment_ids else [],
            total_latency_ms=total_latency_ms,
        )
        vector_store.record_composition_trace(trace)
    except Exception:  # noqa: BLE001 — soft-fail; telemetry never blocks a hook
        pass
