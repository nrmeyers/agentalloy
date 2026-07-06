"""Composition injection for proxy requests.

When the signal layer determines that skill composition is warranted, the
composed prose is injected into the LAST ``role == "user"`` message (the
top-level ``system`` block is prompt-cached and must stay byte-identical, so it
is never touched). Both proxy surfaces inject into the user message:

- the native Anthropic passthrough operates on the raw Anthropic JSON payload
  dict via :func:`inject_into_anthropic_messages`,
- the OpenAI-compatible chat-completions path operates on a typed
  ``list[ProxyMessage]`` via :func:`inject_into_openai_messages`.

Both use the same phase-stamped workflow markers so a stale block can be
detected and replaced when the phase advances.

Public API
----------
inject_into_anthropic_messages
    Inject into the last user message of a raw Anthropic payload dict.
inject_into_openai_messages
    Inject into the last user message of a ``list[ProxyMessage]``.
anthropic_marker_begin / ANTHROPIC_MARKER_END
    Phase-stamped workflow markers shared by both injectors.
BANNER_MARKER_BEGIN / BANNER_MARKER_END
    Non-phase-stamped markers for the one-line per-turn phase banner
    (``kind="banner"``: strip-and-replaced every carrier turn).
anthropic_has_marker
    Cadence helper: is a matching marker already present in a payload?
"""

from __future__ import annotations

import logging
from typing import Any, cast

from agentalloy.api.proxy_models import ProxyMessage

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# User-message injection (both surfaces)
#
# The top-level ``system`` field is prompt-cached and must stay byte-identical,
# so these helpers inject into the LAST ``role == "user"`` message instead.
# ``inject_into_anthropic_messages`` operates on the raw Anthropic JSON payload
# dict; ``inject_into_openai_messages`` operates on a typed list[ProxyMessage].
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

# Banner markers are NOT phase-stamped: the one-line phase banner is strip-and-replaced
# on EVERY carrier turn (the progress count changes turn to turn), so a single
# non-phase-stamped marker family lets the prior banner be removed without knowing the
# phase it carried. Distinct from the workflow and system families so the banner never
# disturbs those blocks.
BANNER_MARKER_BEGIN = "<!-- BEGIN AGENTALLOY-BANNER -->"
BANNER_MARKER_END = "<!-- END AGENTALLOY-BANNER -->"

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


def _strip_banner_block(text: str) -> str:
    """Remove the banner block from *text* (the markers are not phase-stamped)."""
    return _strip_block(text, BANNER_MARKER_BEGIN, BANNER_MARKER_END)


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
    ``kind == "banner"``:
        Uses ``BANNER_MARKER_BEGIN`` .. ``BANNER_MARKER_END``. NOT idempotent:
        any existing banner block is stripped and a fresh one appended last every
        time (the progress count changes turn to turn). ``phase`` is unused for the
        marker (the family is not phase-stamped). The workflow and system blocks are
        never touched.
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
    elif kind == "banner":
        begin, end = BANNER_MARKER_BEGIN, BANNER_MARKER_END
        # Strip-and-replace every turn: no idempotent short-circuit.
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
        if kind == "workflow":
            stripped = _strip_workflow_block(content)
        elif kind == "banner":
            stripped = _strip_banner_block(content)
        else:
            stripped = content
        new_content = f"{stripped}\n\n{new_block}" if stripped else new_block
    elif isinstance(content, list):
        raw_blocks = cast("list[Any]", content)
        blocks: list[dict[str, Any]] = [d for b in raw_blocks if (d := _as_dict(b)) is not None]
        if kind == "workflow":
            # Drop any stale workflow text-block, then append the fresh one.
            blocks = [b for b in blocks if not _text_block_contains(b, _workflow_begin_any())]
        elif kind == "banner":
            # Drop any prior banner text-block, then append the fresh one.
            blocks = [b for b in blocks if not _text_block_contains(b, BANNER_MARKER_BEGIN)]
        new_content = [*blocks, {"type": "text", "text": new_block}]
    else:
        # Unexpected content shape -- leave the payload untouched.
        return payload

    new_message = {**target, "content": new_content}
    new_messages = list(messages)
    new_messages[idx] = new_message
    return {**payload, "messages": new_messages}


# ---------------------------------------------------------------------------
# OpenAI Responses surface (payload["input"] — codex et al.)
#
# A Responses user turn is {"type": "message", "role": "user", "content":
# [{"type": "input_text", "text": ...}]}; `input` may also be a bare string.
# The top-level `instructions` field is the harness's cached system prompt and
# is never touched. Spec: docs/responses-surface.md.
# ---------------------------------------------------------------------------


def _input_text_block_contains(block: dict[str, Any], needle: str) -> bool:
    """True if *block* is an ``input_text`` block whose text contains *needle*."""
    if block.get("type") != "input_text":
        return False
    text = block.get("text")
    return isinstance(text, str) and needle in text


def _last_user_input_index(items: list[Any]) -> int | None:
    """Index of the last user message item in a Responses ``input`` list."""
    for i in range(len(items) - 1, -1, -1):
        item = _as_dict(items[i])
        if item is not None and item.get("type") == "message" and item.get("role") == "user":
            return i
    return None


def _input_item_contains(item: Any, needle: str) -> bool:
    """True if a Responses input item's content contains *needle*."""
    d = _as_dict(item)
    if d is None:
        return False
    content = d.get("content")
    if isinstance(content, str):
        return needle in content
    if isinstance(content, list):
        blocks = cast("list[Any]", content)
        return any(
            _input_text_block_contains(b, needle)
            for b in (_as_dict(x) for x in blocks)
            if b is not None
        )
    return False


def responses_has_marker(
    payload: dict[str, Any], *, kind: str = "workflow", phase: str | None = None
) -> bool:
    """True if a matching marker is present in the payload's ``input`` items."""
    raw = payload.get("input")
    if isinstance(raw, str):
        if kind == "system":
            return SYSTEM_MARKER_BEGIN in raw
        needle = anthropic_marker_begin(phase) if phase is not None else _workflow_begin_any()
        return needle in raw
    if not isinstance(raw, list):
        return False
    items = cast("list[Any]", raw)
    if kind == "system":
        return any(_input_item_contains(i, SYSTEM_MARKER_BEGIN) for i in items)
    needle = anthropic_marker_begin(phase) if phase is not None else _workflow_begin_any()
    return any(_input_item_contains(i, needle) for i in items)


def inject_into_responses_input(
    payload: dict[str, Any], block: str, *, phase: str, kind: str = "workflow"
) -> dict[str, Any]:
    """Inject *block* into the LAST user message item of a Responses payload.

    The Responses-surface sibling of :func:`inject_into_anthropic_messages`:
    same marker families and idempotence/stale-strip semantics, operating on
    ``payload["input"]`` (str or item list). Returns a NEW payload on a real
    injection and the SAME object on every no-op (identity = delivered). The
    top-level ``instructions`` field is never touched.
    """
    if kind == "system":
        begin, end = SYSTEM_MARKER_BEGIN, SYSTEM_MARKER_END
        if responses_has_marker(payload, kind="system"):
            return payload
    elif kind == "banner":
        begin, end = BANNER_MARKER_BEGIN, BANNER_MARKER_END
    else:
        begin, end = anthropic_marker_begin(phase), ANTHROPIC_MARKER_END
        if responses_has_marker(payload, kind="workflow", phase=phase):
            return payload

    new_block = _block_text(begin, block, end)
    raw = payload.get("input")

    if isinstance(raw, str):
        if kind == "workflow":
            stripped = _strip_workflow_block(raw)
        elif kind == "banner":
            stripped = _strip_banner_block(raw)
        else:
            stripped = raw
        new_input: str | list[Any] = f"{stripped}\n\n{new_block}" if stripped else new_block
        return {**payload, "input": new_input}

    if not isinstance(raw, list):
        return payload
    items = cast("list[Any]", raw)
    idx = _last_user_input_index(items)
    if idx is None:
        return payload
    target = _as_dict(items[idx])
    if target is None:
        return payload

    content = target.get("content")
    if isinstance(content, str):
        if kind == "workflow":
            stripped = _strip_workflow_block(content)
        elif kind == "banner":
            stripped = _strip_banner_block(content)
        else:
            stripped = content
        new_content: str | list[dict[str, Any]] = (
            f"{stripped}\n\n{new_block}" if stripped else new_block
        )
    elif isinstance(content, list):
        raw_blocks = cast("list[Any]", content)
        blocks: list[dict[str, Any]] = [d for b in raw_blocks if (d := _as_dict(b)) is not None]
        if kind == "workflow":
            blocks = [b for b in blocks if not _input_text_block_contains(b, _workflow_begin_any())]
        elif kind == "banner":
            blocks = [b for b in blocks if not _input_text_block_contains(b, BANNER_MARKER_BEGIN)]
        new_content = [*blocks, {"type": "input_text", "text": new_block}]
    else:
        return payload

    new_item = {**target, "content": new_content}
    new_items = list(items)
    new_items[idx] = new_item
    return {**payload, "input": new_items}


def _last_user_message_index(messages: list[ProxyMessage]) -> int | None:
    """Index of the last ``role == "user"`` message in a typed list, or None."""
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].role == "user":
            return i
    return None


def inject_into_openai_messages(
    messages: list[ProxyMessage], block: str, *, phase: str, kind: str = "workflow"
) -> list[ProxyMessage] | None:
    """Inject *block* into the LAST ``role == "user"`` message of a typed list.

    The OpenAI-surface sibling of :func:`inject_into_anthropic_messages`: same
    phase-stamped workflow markers (``anthropic_marker_begin(phase)`` ..
    ``ANTHROPIC_MARKER_END``), same idempotence + stale-strip semantics, but it
    operates on ``list[ProxyMessage]`` rather than a raw payload dict.

    Returns a NEW list with only the target user message replaced (via
    ``model_copy``); the system message and every other message are left
    untouched and the input list is never mutated. Returns ``None`` on every
    no-op so the caller can treat non-None as "delivered":

    - no ``role == "user"`` message,
    - the target already carries the current-phase begin marker (idempotent),
    - an unexpected content shape (neither ``str`` nor block ``list``).

    ``kind == "banner"`` uses the non-phase-stamped banner markers and is NOT
    idempotent: any existing banner block is stripped and a fresh one appended last
    every time (the progress count changes turn to turn), so it returns ``None`` only
    on no-user-message or an unexpected content shape. The workflow and system blocks
    are never touched.
    """
    idx = _last_user_message_index(messages)
    if idx is None:
        return None

    if kind == "banner":
        begin, end = BANNER_MARKER_BEGIN, BANNER_MARKER_END
    else:
        begin, end = anthropic_marker_begin(phase), ANTHROPIC_MARKER_END
    target = messages[idx]
    content = target.content

    # Idempotent: current-phase block already present in the target.
    if isinstance(content, str):
        if kind != "banner" and begin in content:
            return None
        if kind == "workflow":
            stripped = _strip_workflow_block(content)
        elif kind == "banner":
            stripped = _strip_banner_block(content)
        else:
            stripped = content
        new_block = _block_text(begin, block, end)
        new_content: str | list[dict[str, Any]] = (
            f"{stripped}\n\n{new_block}" if stripped else new_block
        )
    elif isinstance(content, list):
        # ProxyMessage.content is str | list[dict[str, Any]] | None, so the list
        # branch is already list[dict[str, Any]] — no cast needed.
        blocks = content
        if kind != "banner" and any(_text_block_contains(b, begin) for b in blocks):
            return None
        if kind == "workflow":
            # Drop any stale workflow text-block, then append the fresh one.
            blocks = [b for b in blocks if not _text_block_contains(b, _workflow_begin_any())]
        elif kind == "banner":
            # Drop any prior banner text-block, then append the fresh one.
            blocks = [b for b in blocks if not _text_block_contains(b, BANNER_MARKER_BEGIN)]
        new_block = _block_text(begin, block, end)
        new_content = [*blocks, {"type": "text", "text": new_block}]
    else:
        # Unexpected content shape (e.g. None) -- leave the list untouched.
        return None

    new_messages = list(messages)
    new_messages[idx] = target.model_copy(update={"content": new_content})
    return new_messages
