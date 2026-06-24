"""Composition injection for proxy requests.

When the signal layer determines that skill composition is warranted, this
module runs the compose engine and injects the result into the system message
of the incoming proxy request.

Public API
----------
MARKER_BEGIN
MARKER_END
    Marker constants used to delimit the AgentAlloy context block.

inject_composed_output
    Inject ComposedResult.output into the system message.

extract_system_message / replace_system_message
    Low-level helpers for finding/replacing system messages in the message list.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

from agentalloy.api.compose_models import ComposeRequest, EmptyResult, Phase
from agentalloy.api.proxy_models import ProxyMessage, ProxyRequest
from agentalloy.api.proxy_signal import SignalResult

if TYPE_CHECKING:
    from agentalloy.orchestration.compose import ComposeOrchestrator

logger = logging.getLogger(__name__)

# Sentinel markers delimiting the AgentAlloy context block
MARKER_BEGIN = "<!-- BEGIN AGENTALLOY-CONTEXT -->"
MARKER_END = "<!-- END AGENTALLOY-CONTEXT -->"


def _build_marker_block(output: str) -> str:
    """Wrap *output* in the AgentAlloy context markers."""
    return f"{MARKER_BEGIN}\n{output}\n{MARKER_END}"


def extract_system_message(messages: list[ProxyMessage]) -> ProxyMessage | None:
    """Return the first system message, or None."""
    for msg in messages:
        if msg.role == "system":
            return msg
    return None


def replace_system_message(messages: list[ProxyMessage], new_msg: ProxyMessage) -> None:
    """Replace the first system message in-place."""
    for i, msg in enumerate(messages):
        if msg.role == "system":
            messages[i] = new_msg
            return


def inject_composed_output(request: ProxyRequest, output: str) -> ProxyRequest:
    """Inject *output* into the system message of *request*.

    Injection logic:
    1. If a system message exists and already contains the marker block:
       replace just the block (idempotent).
    2. If a system message exists without markers: append the block.
    3. If no system message: prepend one containing just the block.

    Returns a new ProxyRequest with modified messages.
    """
    marker_block = _build_marker_block(output)
    sys_msg = extract_system_message(request.messages)

    if sys_msg is None:
        # No system message -- prepend one
        new_messages = [ProxyMessage(role="system", content=marker_block)]
        new_messages.extend(request.messages)
    elif isinstance(sys_msg.content, str) and MARKER_BEGIN in sys_msg.content:
        # Marker block already exists -- replace it (idempotent)
        old_block = _extract_marker_block(sys_msg.content)
        new_content = sys_msg.content.replace(old_block, marker_block)
        new_sys = ProxyMessage(role="system", content=new_content)
        new_messages = list(request.messages)
        replace_system_message(new_messages, new_sys)
    elif isinstance(sys_msg.content, str):
        # System message exists, no markers -- append
        new_content = sys_msg.content + "\n\n" + marker_block
        new_sys = ProxyMessage(role="system", content=new_content)
        new_messages = list(request.messages)
        replace_system_message(new_messages, new_sys)
    else:
        # System message has list content or None -- prepend a new system message
        new_messages = [ProxyMessage(role="system", content=marker_block)]
        new_messages.extend(request.messages)

    return ProxyRequest(
        model=request.model,
        messages=new_messages,
        stream=request.stream,
        temperature=request.temperature,
        max_tokens=request.max_tokens,
        top_p=request.top_p,
        presence_penalty=request.presence_penalty,
        frequency_penalty=request.frequency_penalty,
        n=request.n,
        user=request.user,
        metadata=request.metadata,
    )


def _extract_marker_block(content: str) -> str:
    """Extract the existing marker block from system message content."""
    begin = content.find(MARKER_BEGIN)
    end = content.find(MARKER_END)
    if begin != -1 and end != -1:
        return content[begin : end + len(MARKER_END)]
    return ""


async def compose_and_inject(
    request: ProxyRequest,
    signal: SignalResult,
    orchestrator: ComposeOrchestrator,
) -> ProxyRequest:
    """Run composition and inject result into the system message.

    If signal.should_compose is False, returns the request unchanged.
    If composition fails or returns EmptyResult, also returns the request
    unchanged (soft-fail -- composition never blocks the proxy).

    Args:
        request: the incoming proxy request
        signal: result from evaluate_signal()
        orchestrator: the ComposeOrchestrator instance

    Returns:
        Modified ProxyRequest with injected system message, or the
        original request if composition was skipped or returned nothing.
    """
    if not signal.should_compose:
        return request

    task = signal.task or ""
    phase = signal.phase

    # Build ComposeRequest
    # signal.phase may not be a valid Phase literal if it's something
    # unexpected; fall back to "build" as a safe default.
    compose_phase: Phase = (
        phase
        if phase in ("intake", "spec", "design", "build", "qa", "ship", "sdd-fast")
        else "build"
    )

    compose_req = ComposeRequest(
        task=task,
        phase=compose_phase,
        domain_tags=signal.domain_tags or None,
    )

    # Gate advisories (e.g. "intent fired but the exit artifact is missing")
    # are surfaced even when no domain fragments match, so the agent always
    # learns what to produce to advance the phase.
    advisory_block = ""
    if signal.advisories:
        advisory_block = (
            "[agentalloy-eval]\n" + "\n".join(signal.advisories) + "\n[/agentalloy-eval]"
        )

    try:
        result = await orchestrator.compose(
            compose_req,
            repo=signal.repo,
            session_key=signal.session_key,
            session_source=signal.session_source,
        )
        domain_output = "" if isinstance(result, EmptyResult) else result.output
    except Exception:
        logger.warning("Composition failed -- passing through unchanged", exc_info=True)
        domain_output = ""

    parts = [p for p in (advisory_block, domain_output) if p]
    if not parts:
        # Nothing to inject (no domain fragments, no advisory) -- passthrough.
        return request

    try:
        return inject_composed_output(request, "\n\n".join(parts))
    except Exception:
        logger.warning("Injection failed -- passing through unchanged", exc_info=True)
        return request


# ---------------------------------------------------------------------------
# Native Anthropic Messages passthrough injection
#
# For the native Anthropic passthrough the top-level ``system`` field is
# prompt-cached and must stay byte-identical, so these helpers inject into the
# LAST ``role == "user"`` message instead. They operate on the raw Anthropic
# JSON payload dict, never on ProxyRequest.
# ---------------------------------------------------------------------------


# Workflow markers are phase-stamped so a stale block can be detected and
# replaced when the phase advances.
def anthropic_marker_begin(phase: str) -> str:
    """Phase-stamped opening marker for a workflow context block."""
    return f"<!-- BEGIN AGENTALLOY-CONTEXT phase={phase} -->"


ANTHROPIC_MARKER_END = "<!-- END AGENTALLOY-CONTEXT -->"

# System markers are not phase-stamped: they are injected at most once per
# session.
SYSTEM_MARKER_BEGIN = "<!-- BEGIN AGENTALLOY-SYSTEM -->"
SYSTEM_MARKER_END = "<!-- END AGENTALLOY-SYSTEM -->"

# Matches the phase value inside a workflow begin marker.
_WORKFLOW_BEGIN_PREFIX = "<!-- BEGIN AGENTALLOY-CONTEXT phase="
_WORKFLOW_BEGIN_SUFFIX = " -->"


def _workflow_begin_any() -> str:
    """Marker-begin prefix shared by every workflow phase."""
    return _WORKFLOW_BEGIN_PREFIX


def _find_marker_phase(text: str) -> str | None:
    """Return the phase of the first workflow marker in *text*, or None."""
    start = text.find(_WORKFLOW_BEGIN_PREFIX)
    if start == -1:
        return None
    value_start = start + len(_WORKFLOW_BEGIN_PREFIX)
    value_end = text.find(_WORKFLOW_BEGIN_SUFFIX, value_start)
    if value_end == -1:
        return None
    return text[value_start:value_end]


def _strip_block(text: str, begin: str, end: str) -> str:
    """Remove the first ``begin``..``end`` block (inclusive) from *text*.

    Also strips the ``\\n\\n`` separator that ``inject`` writes before the
    block so repeated stale-replace cycles do not accumulate blank lines.
    """
    b = text.find(begin)
    e = text.find(end, b + len(begin)) if b != -1 else -1
    if b == -1 or e == -1:
        return text
    block_end = e + len(end)
    # Absorb a preceding "\n\n" separator if present.
    sep_start = b
    if text[max(0, b - 2) : b] == "\n\n":
        sep_start = b - 2
    return (text[:sep_start] + text[block_end:]).strip()


def _strip_workflow_block(text: str) -> str:
    """Remove any workflow block (regardless of phase) from *text*."""
    phase = _find_marker_phase(text)
    if phase is None:
        return text
    return _strip_block(text, anthropic_marker_begin(phase), ANTHROPIC_MARKER_END)


def _block_text(begin: str, block: str, end: str) -> str:
    """The wrapped marker block as it is written into content."""
    return f"{begin}\n{block}\n{end}"


def _as_dict(obj: Any) -> dict[str, Any] | None:
    """Narrow an untrusted JSON value to a ``dict[str, Any]`` or None.

    Keeps pyright-strict from leaking ``dict[Unknown, Unknown]`` at every call
    site that probes the raw payload.
    """
    return cast("dict[str, Any]", obj) if isinstance(obj, dict) else None


def _last_user_index(messages: list[Any]) -> int | None:
    """Index of the last ``role == "user"`` message, or None."""
    for i in range(len(messages) - 1, -1, -1):
        msg = _as_dict(messages[i])
        if msg is not None and msg.get("role") == "user":
            return i
    return None


def _text_block_contains(block: dict[str, Any], needle: str) -> bool:
    """True if *block* is a text block whose text contains *needle*."""
    if block.get("type") != "text":
        return False
    text = block.get("text")
    return isinstance(text, str) and needle in text


def _message_contains(message: Any, needle: str) -> bool:
    """True if a user message's content contains *needle* (str or block list)."""
    msg = _as_dict(message)
    if msg is None:
        return False
    content = msg.get("content")
    if isinstance(content, str):
        return needle in content
    if isinstance(content, list):
        blocks = cast("list[Any]", content)
        return any(
            _text_block_contains(d, needle) for d in (_as_dict(b) for b in blocks) if d is not None
        )
    return False


def anthropic_has_marker(
    payload: dict[str, Any], *, kind: str = "workflow", phase: str | None = None
) -> bool:
    """True if a matching marker is present in the payload's messages.

    ``kind='workflow'`` with ``phase=None`` matches ANY workflow phase; with a
    phase it matches only that phase. ``kind='system'`` matches the system
    marker in any message. Cadence helper for the router.
    """
    raw = payload.get("messages")
    if not isinstance(raw, list):
        return False
    messages = cast("list[Any]", raw)

    if kind == "system":
        return any(_message_contains(m, SYSTEM_MARKER_BEGIN) for m in messages)

    needle = anthropic_marker_begin(phase) if phase is not None else _workflow_begin_any()
    return any(_message_contains(m, needle) for m in messages)


def inject_into_anthropic_messages(
    payload: dict[str, Any], block: str, *, phase: str, kind: str = "workflow"
) -> dict[str, Any]:
    """Inject *block* into the LAST ``role == "user"`` message.

    Returns a NEW payload (the dict is copied and the ``messages`` list plus the
    single mutated message are rebuilt). The top-level ``system`` field and
    every other message are left untouched.

    ``kind == "workflow"``:
        Uses phase-stamped markers ``anthropic_marker_begin(phase)`` ..
        ``ANTHROPIC_MARKER_END``. Idempotent for the current phase; a stale
        block for a different phase is stripped before injecting.
    ``kind == "system"``:
        Uses ``SYSTEM_MARKER_BEGIN`` .. ``SYSTEM_MARKER_END``. Injected at most
        once per session: if any user message already carries a system marker,
        the payload is returned unchanged.
    """
    raw = payload.get("messages")
    if not isinstance(raw, list):
        return payload
    messages = cast("list[Any]", raw)

    idx = _last_user_index(messages)
    if idx is None:
        return payload

    if kind == "system":
        begin, end = SYSTEM_MARKER_BEGIN, SYSTEM_MARKER_END
        # Once per session: any existing system marker short-circuits.
        if anthropic_has_marker(payload, kind="system"):
            return payload
    else:
        begin, end = anthropic_marker_begin(phase), ANTHROPIC_MARKER_END
        # Idempotent: current-phase block already present.
        if anthropic_has_marker(payload, kind="workflow", phase=phase):
            return payload

    target = _as_dict(messages[idx])
    if target is None:
        return payload

    new_block = _block_text(begin, block, end)
    content = target.get("content")
    new_content: str | list[dict[str, Any]]

    if isinstance(content, str):
        stripped = _strip_workflow_block(content) if kind == "workflow" else content
        new_content = f"{stripped}\n\n{new_block}" if stripped else new_block
    elif isinstance(content, list):
        raw_blocks = cast("list[Any]", content)
        blocks: list[dict[str, Any]] = [d for b in raw_blocks if (d := _as_dict(b)) is not None]
        if kind == "workflow":
            # Drop any stale workflow text-block, then append the fresh one.
            blocks = [b for b in blocks if not _text_block_contains(b, _workflow_begin_any())]
        new_content = [*blocks, {"type": "text", "text": new_block}]
    else:
        # Unexpected content shape -- leave the payload untouched.
        return payload

    new_message = {**target, "content": new_content}
    new_messages = list(messages)
    new_messages[idx] = new_message
    return {**payload, "messages": new_messages}
