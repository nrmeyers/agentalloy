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
from typing import TYPE_CHECKING

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
        if phase in ("intake", "spec", "design", "qa", "build", "ops", "meta", "governance", "ship")
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
        result = await orchestrator.compose(compose_req)
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
