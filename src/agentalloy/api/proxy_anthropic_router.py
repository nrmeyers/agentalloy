"""Anthropic Messages API proxy router.

Translates POST /v1/messages (Anthropic format) to the existing OpenAI-compatible
proxy pipeline and converts the response back to Anthropic format.

Scope: text-only and tool-use (function calling), non-streaming and streaming.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from agentalloy.api.proxy_anthropic_models import (
    AnthropicContentBlock,
    AnthropicRequest,
    AnthropicResponse,
)
from agentalloy.api.proxy_models import ProxyMessage, ProxyRequest
from agentalloy.api.proxy_router import (  # pyright: ignore[reportPrivateUsage]
    _build_payload,
    _upstream_not_configured_error,
    get_settings_for_proxy,
    get_upstream_client,
)
from agentalloy.api.upstream.error_sse import (
    make_http_error_sse,
    make_network_error_sse,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Translation helpers
# ---------------------------------------------------------------------------


def _anthropic_tool_to_openai(tool: dict[str, Any]) -> dict[str, Any]:
    """Convert a single Anthropic tool definition to OpenAI format.

    Anthropic:  { name, description, input_schema }
    OpenAI:     { type: "function", function: { name, description, parameters } }
    """
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description"),
            "parameters": tool.get("input_schema", {}),
        },
    }


def _anthropic_tool_choice_to_openai(
    tc: dict[str, Any] | None,
) -> str | dict[str, Any] | None:
    """Convert an Anthropic tool_choice to OpenAI format.

    Anthropic:  { type: "auto" | "any" | "tool", name: str | None }
    OpenAI:     "auto" | "none" | "required" | { type: "function", function: { name } }
    """
    if tc is None:
        return None
    t = tc.get("type")
    if t == "auto":
        return "auto"
    if t == "any":
        return "required"
    if t == "tool":
        return {"type": "function", "function": {"name": tc.get("name")}}
    return None


def _anthropic_message_to_openai(
    msg: dict[str, Any],
) -> ProxyMessage:
    """Convert a single Anthropic message to an OpenAI ProxyMessage.

    Handles:
    - role mapping (Anthropic "user"/"assistant" → OpenAI "user"/"assistant")
    - content: str → content: str
    - content: list[blocks] → content: list[dict] (passthrough for text/tool_use blocks)
    - tool_calls: list[{id, name, input}] → OpenAI tool_calls
    - tool_result role → OpenAI tool role
    """
    role = msg.get("role", "user")
    content = msg.get("content")

    # --- tool_calls extraction ---
    tool_calls: list[dict[str, Any]] | None = None
    tc_list = msg.get("tool_calls")
    if tc_list:
        tool_calls = []
        for tc in tc_list:
            tool_calls.append(
                {
                    "id": tc.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                    "type": "function",
                    "function": {
                        "name": tc.get("name", ""),
                        "arguments": json.dumps(tc.get("input", {})),
                    },
                }
            )

    # --- content normalisation ---
    if isinstance(content, str):
        # Plain text content
        if tool_calls:
            return ProxyMessage(role=role, content=content, tool_calls=tool_calls)
        return ProxyMessage(role=role, content=content)

    if isinstance(content, list):
        # Content-block array
        # Check if any block is a tool_use — if so, preserve the array
        has_tool_use = any(
            isinstance(block, dict) and block.get("type") == "tool_use" for block in content
        )
        if has_tool_use:
            return ProxyMessage(role=role, content=content)
        # No tool_use blocks — merge text blocks into a single string
        text_parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(block.get("text", ""))
        return ProxyMessage(role=role, content="".join(text_parts) or None)

    # content is None or unexpected type — still include tool_calls if present
    if tool_calls:
        return ProxyMessage(role=role, content=content, tool_calls=tool_calls)
    return ProxyMessage(role=role, content=content)


def _tool_result_to_str(content: object) -> str:
    """Flatten an Anthropic tool_result's content to an OpenAI tool-message string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(str(b.get("text", "")))
            elif isinstance(b, dict):
                parts.append(json.dumps(b))
            else:
                parts.append(str(b))
        return "".join(parts)
    return json.dumps(content) if content is not None else ""


def _expand_tool_results(msg_dict: dict[str, Any]) -> list[ProxyMessage] | None:
    """Expand tool_result blocks into OpenAI tool-role messages (one per result).

    An Anthropic user message carries tool outputs as
    ``content=[{type:'tool_result', tool_use_id, content}, ...]``. Each becomes a
    ``role='tool'`` message; sibling text blocks are preserved as a user message.
    Returns None when the message has no tool_result blocks (normal path).
    """
    content = msg_dict.get("content")
    if not isinstance(content, list):
        return None
    results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
    if not results:
        return None
    out: list[ProxyMessage] = [
        ProxyMessage(
            role="tool",
            tool_call_id=b.get("tool_use_id"),
            content=_tool_result_to_str(b.get("content")),
        )
        for b in results
    ]
    text = "".join(
        str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") == "text"
    )
    if text:
        out.append(ProxyMessage(role="user", content=text))
    return out


def _anthropic_to_openai(request: AnthropicRequest) -> ProxyRequest:
    """Convert an Anthropic Messages request to an OpenAI ProxyRequest.

    - ``system``: string or content-block list → ``{"role": "system", "content": ...}`` prepended
    - ``messages``: role/content/tool_calls pass-through with format conversion
    - ``stream``, ``temperature``, ``top_p``: pass-through
    - ``model``: pass-through (proxy handles model resolution)
    - ``max_tokens``: mapped to ``max_tokens`` on ProxyRequest
    - ``tools``: Anthropic tool format → OpenAI function-calling tool format
    - ``tool_choice``: Anthropic tool_choice → OpenAI tool_choice
    """
    messages: list[ProxyMessage] = []

    # System message
    if request.system is not None:
        if isinstance(request.system, str):
            messages.append(ProxyMessage(role="system", content=request.system))
        else:
            # Content-block array for system — merge to string. The remaining
            # type is list[AnthropicContentBlock] (None and str are eliminated
            # by the branches above), so no isinstance(list) check is needed.
            text_parts: list[str] = []
            for block in request.system:
                if isinstance(block, AnthropicContentBlock) and block.type == "text":
                    text_parts.append(block.text or "")
            messages.append(ProxyMessage(role="system", content="".join(text_parts) or None))

    # User / assistant / tool messages
    for m in request.messages:
        msg_dict = m.model_dump(mode="json", exclude_none=True)
        tool_msgs = _expand_tool_results(msg_dict)
        if tool_msgs is not None:
            messages.extend(tool_msgs)
        else:
            messages.append(_anthropic_message_to_openai(msg_dict))

    # Tools
    tools: list[dict[str, Any]] | None = None
    if request.tools:
        tools = [_anthropic_tool_to_openai(t.model_dump(mode="json")) for t in request.tools]

    # Tool choice
    tool_choice: str | dict[str, Any] | None = None
    if request.tool_choice is not None:
        tool_choice = _anthropic_tool_choice_to_openai(request.tool_choice.model_dump(mode="json"))

    return ProxyRequest(
        model=request.model,
        messages=messages,
        stream=request.stream,
        temperature=request.temperature,
        max_tokens=request.max_tokens,
        top_p=request.top_p,
        tools=tools,
        tool_choice=tool_choice,
    )


def _openai_to_anthropic(openai_body: dict[str, Any], model: str) -> dict[str, Any]:
    """Convert a non-streaming OpenAI chat completion response to Anthropic format.

    - ``choices[0].message.content`` → ``content: [{type: "text", text: ...}]``
    - ``choices[0].message.tool_calls`` → ``content: [{type: "tool_use", ...}]``
    - ``usage.prompt_tokens`` / ``completion_tokens`` → ``usage.input_tokens`` / ``output_tokens``
    - ``finish_reason`` → ``stop_reason`` (``"stop"`` → ``"end_turn"``,
      ``"length"`` → ``"max_tokens"``, ``"tool_calls"`` → ``"tool_use"``)
    """
    choices: list[dict[str, Any]] | None = openai_body.get("choices")
    if not choices:
        return {
            "id": openai_body.get("id") or f"msg_{uuid.uuid4().hex[:24]}",
            "model": openai_body.get("model") or model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "error": {
                "type": "invalid_request_error",
                "message": "Upstream returned no choices",
            },
        }
    choice: dict[str, Any] = choices[0]
    message: dict[str, Any] = choice.get("message") or {}

    finish: str | None = choice.get("finish_reason")
    stop_reason: str | None = None
    if finish == "stop":
        stop_reason = "end_turn"
    elif finish == "length":
        stop_reason = "max_tokens"
    elif finish == "tool_calls":
        stop_reason = "tool_use"

    # Build content blocks
    content_blocks: list[dict[str, Any]] = []

    # Text content
    text: str | None = message.get("content")
    if text:
        content_blocks.append({"type": "text", "text": text})

    # Tool calls
    tc_list: list[dict[str, Any]] | None = message.get("tool_calls")
    if tc_list:
        for tc in tc_list:
            fn = tc.get("function") or {}
            try:
                input_data: dict[str, Any] = json.loads(fn.get("arguments", "{}"))
            except (json.JSONDecodeError, TypeError):
                input_data = {}
            content_blocks.append(
                {
                    "type": "tool_use",
                    "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:24]}"),
                    "name": fn.get("name", ""),
                    "input": input_data,
                }
            )

    if not content_blocks:
        content_blocks.append({"type": "text", "text": ""})

    usage_raw: dict[str, Any] = openai_body.get("usage") or {}
    usage: dict[str, Any] = {
        "input_tokens": usage_raw.get("prompt_tokens", 0),
        "output_tokens": usage_raw.get("completion_tokens", 0),
    }

    response = AnthropicResponse(
        id=openai_body.get("id") or f"msg_{uuid.uuid4().hex[:24]}",
        content=[AnthropicContentBlock(**cb) for cb in content_blocks],
        model=openai_body.get("model") or model,
        stop_reason=stop_reason,
        stop_sequence=None,
        usage=usage,
    )
    return response.model_dump()


def _openai_stream_to_anthropic(
    openai_chunks: list[dict[str, Any]], model: str
) -> list[dict[str, Any]]:
    """Convert a sequence of OpenAI SSE chunks to Anthropic SSE events.

    Mapping:
    - First chunk → ``message_start`` + ``content_block_start``
    - Text content chunks → ``content_block_delta``
    - Tool call deltas → ``content_block_delta`` (tool_use type)
    - Last chunk (finish_reason set) → ``content_block_stop``
      + ``message_delta`` (with output_tokens) + ``message_stop``

    Usage note: in Anthropic streaming, usage goes in ``message_delta``,
    NOT in ``message_stop``.
    """
    events: list[dict[str, Any]] = []
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    output_tokens: int = 0
    input_tokens: int = 0
    first = True
    text_started = False
    stop_reason = "end_turn"
    # Accumulate tool-call deltas keyed by index
    tool_call_deltas: dict[int, dict[str, Any]] = {}

    for chunk in openai_chunks:
        choices: list[dict[str, Any]] = chunk.get("choices") or []
        if not choices:
            usage: dict[str, Any] = chunk.get("usage") or {}
            if usage:
                input_tokens = int(usage.get("prompt_tokens") or input_tokens)
                output_tokens = int(usage.get("completion_tokens") or output_tokens)
            continue

        choice: dict[str, Any] = choices[0]
        delta: dict[str, Any] = choice.get("delta") or {}
        finish: str | None = choice.get("finish_reason")
        tc_list: list[dict[str, Any]] | None = delta.get("tool_calls")

        text: str | None = delta.get("content")

        if first:
            events.append(
                {
                    "type": "message_start",
                    "message": {
                        "id": msg_id,
                        "type": "message",
                        "role": "assistant",
                        "content": [],
                        "model": model,
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 0, "output_tokens": 0},
                    },
                }
            )
            first = False

        if tc_list:
            for i, tc in enumerate(tc_list):
                idx = tc.get("index", i)
                base = tool_call_deltas.get(idx, {})
                if tc.get("id"):
                    base["id"] = tc["id"]
                fn = tc.get("function", {})
                if fn.get("name"):
                    base["name"] = fn["name"]
                if fn.get("arguments"):
                    base.setdefault("partial_json", "")
                    base["partial_json"] += fn["arguments"]
                tool_call_deltas[idx] = base

        if text:
            # Open the text block before its first delta (Anthropic protocol
            # requires content_block_start before any content_block_delta).
            if not text_started:
                events.append(
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    }
                )
                text_started = True
            events.append(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": text},
                }
            )

        if finish:
            stop_reason = "end_turn" if finish == "stop" else "max_tokens"
            if finish == "tool_calls":
                stop_reason = "tool_use"

        usage: dict[str, Any] = chunk.get("usage") or {}
        if usage:
            input_tokens = int(usage.get("prompt_tokens") or input_tokens)
            output_tokens = int(usage.get("completion_tokens") or output_tokens)

    # Emit tool_use content blocks from accumulated deltas
    for idx, tc_data in sorted(tool_call_deltas.items()):
        partial_json = tc_data.get("partial_json", "")
        events.append(
            {
                "type": "content_block_start",
                "index": idx + 1,
                "content_block": {
                    "type": "tool_use",
                    "id": tc_data.get("id", f"toolu_{uuid.uuid4().hex[:24]}"),
                    "name": tc_data.get("name", ""),
                },
            }
        )
        events.append(
            {
                "type": "content_block_delta",
                "index": idx + 1,
                "delta": {"type": "input_json_delta", "partial_json": partial_json},
            }
        )
        events.append(
            {"type": "content_block_stop", "index": idx + 1},
        )

    if first:
        # Empty stream
        events.append(
            {
                "type": "message_start",
                "message": {
                    "id": msg_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": model,
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            }
        )

    # Final content_block_stop for text block (index 0)
    events.append({"type": "content_block_stop", "index": 0})
    events.append(
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": output_tokens},
        }
    )
    events.append({"type": "message_stop"})
    return events


# ---------------------------------------------------------------------------
# Interleaved streaming state machine (Task 7)
# ---------------------------------------------------------------------------


def _openai_stream_to_anthropic_interleaved(
    openai_chunks: list[dict[str, Any]], model: str
) -> list[dict[str, Any]]:
    """Convert interleaved OpenAI SSE chunks to Anthropic SSE events.

    This is the Task 7 implementation: a streaming state machine that
    properly handles text and tool_calls arriving in the same OpenAI
    chunk (interleaved deltas).

    Anthropic requires sequential content blocks:
      content_block_start (text) → content_block_delta (text) →
      content_block_stop (text) → content_block_start (tool_use) →
      content_block_delta (input_json) → content_block_stop (tool_use) →
      content_block_start (tool_use) → ...

    OpenAI sends interleaved deltas:
      delta: {content: "Hello"}  # text
      delta: {tool_calls: [{index:0, function:{name:"search"}}]}  # tool start
      delta: {tool_calls: [{index:0, function:{arguments:'{"q'}}]}  # tool arg
      delta: {content: " world"}  # more text after tool
      delta: {tool_calls: [{index:1, function:{name:"browse"}}]}  # another tool

    This function serializes interleaved deltas into proper sequential
    content blocks, emitting content_block_stop for the current block
    before starting a new one.

    Handles:
    - Partial JSON chunks (arguments arrive in multiple deltas)
    - Interleaved text and tool_calls
    - Multiple tool_calls with different indices
    - Text before, between, and after tool_calls
    """
    events: list[dict[str, Any]] = []
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    output_tokens: int = 0
    input_tokens: int = 0
    first = True
    stop_reason = "end_turn"

    # Track tool call state keyed by OpenAI index
    # Each entry: {id, name, partial_json, emitted_start: bool}
    tool_states: dict[int, dict[str, Any]] = {}

    # Track whether we have pending text content (not yet stopped)
    pending_text = False

    def _emit_text_stop() -> None:
        """Emit content_block_stop for text block (index 0) if pending."""
        nonlocal pending_text
        if pending_text:
            events.append({"type": "content_block_stop", "index": 0})
            pending_text = False

    def _emit_tool_stop(idx: int) -> None:
        """Emit content_block_stop for a specific tool_use index."""
        if idx in tool_states and tool_states[idx].get("emitted_start"):
            events.append({"type": "content_block_stop", "index": idx + 1})
            tool_states[idx]["emitted_start"] = False

    for chunk in openai_chunks:
        choices: list[dict[str, Any]] = chunk.get("choices") or []
        if not choices:
            usage: dict[str, Any] = chunk.get("usage") or {}
            if usage:
                input_tokens = int(usage.get("prompt_tokens") or input_tokens)
                output_tokens = int(usage.get("completion_tokens") or output_tokens)
            continue

        choice: dict[str, Any] = choices[0]
        delta: dict[str, Any] = choice.get("delta") or {}
        finish: str | None = choice.get("finish_reason")
        tc_list: list[dict[str, Any]] | None = delta.get("tool_calls")
        text: str | None = delta.get("content")

        if first:
            events.append(
                {
                    "type": "message_start",
                    "message": {
                        "id": msg_id,
                        "type": "message",
                        "role": "assistant",
                        "content": [],
                        "model": model,
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 0, "output_tokens": 0},
                    },
                }
            )
            first = False

        # --- Process tool calls ---
        if tc_list:
            # Stop any pending text before starting tool_use
            _emit_text_stop()

            for i, tc in enumerate(tc_list):
                tc_idx = tc.get("index", i)
                fn = tc.get("function", {})

                if tc_idx not in tool_states:
                    tool_states[tc_idx] = {
                        "id": "",
                        "name": "",
                        "partial_json": "",
                        "emitted_start": False,
                    }
                ts = tool_states[tc_idx]

                # Emit tool_use content_block_start on first arrival
                if not ts["emitted_start"]:
                    if tc.get("id"):
                        ts["id"] = tc["id"]
                    if fn.get("name"):
                        ts["name"] = fn["name"]
                    events.append(
                        {
                            "type": "content_block_start",
                            "index": tc_idx + 1,
                            "content_block": {
                                "type": "tool_use",
                                "id": ts["id"] or f"toolu_{uuid.uuid4().hex[:24]}",
                                "name": ts["name"],
                            },
                        }
                    )
                    ts["emitted_start"] = True

                # Accumulate partial JSON arguments
                if fn.get("arguments"):
                    ts["partial_json"] += fn["arguments"]
                    events.append(
                        {
                            "type": "content_block_delta",
                            "index": tc_idx + 1,
                            "delta": {
                                "type": "input_json_delta",
                                "partial_json": fn["arguments"],
                            },
                        }
                    )

        # --- Process text content ---
        if text:
            # If we had a pending tool_use, stop it before text
            for tc_idx in list(tool_states.keys()):
                if tool_states[tc_idx].get("emitted_start"):
                    _emit_tool_stop(tc_idx)

            # Open the text block before its first delta (Anthropic protocol
            # requires content_block_start before any content_block_delta).
            if not pending_text:
                events.append(
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    }
                )
            events.append(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": text},
                }
            )
            pending_text = True

        # --- Handle finish_reason ---
        if finish:
            # Stop all pending blocks
            _emit_text_stop()
            for tc_idx in list(tool_states.keys()):
                _emit_tool_stop(tc_idx)

            stop_reason = "end_turn" if finish == "stop" else "max_tokens"
            if finish == "tool_calls":
                stop_reason = "tool_use"

        # --- Handle usage chunks ---
        usage: dict[str, Any] = chunk.get("usage") or {}
        if usage:
            input_tokens = int(usage.get("prompt_tokens") or input_tokens)
            output_tokens = int(usage.get("completion_tokens") or output_tokens)

    # --- End of stream: stop any remaining blocks ---
    _emit_text_stop()
    for tc_idx in list(tool_states.keys()):
        _emit_tool_stop(tc_idx)

    if first:
        # Empty stream — emit minimal structure
        events.append(
            {
                "type": "message_start",
                "message": {
                    "id": msg_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": model,
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            }
        )

    # Final message events
    events.append(
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": output_tokens},
        }
    )
    events.append({"type": "message_stop"})
    return events


# ---------------------------------------------------------------------------
# Streaming translation
# ---------------------------------------------------------------------------


def _stream_anthropic_response(
    upstream: httpx.AsyncClient,
    payload: dict[str, Any],
    model: str,
) -> StreamingResponse:
    """Stream an Anthropic-formatted SSE response from an upstream OpenAI endpoint.

    Converts OpenAI SSE chunks to Anthropic SSE events incrementally as they arrive,
    yielding each event immediately. Uses the same interleaved state machine as
    _openai_stream_to_anthropic_interleaved but yields events in real-time.

    Properly handles:
    - Interleaved text and tool_calls arriving in the same chunk
    - Partial JSON argument chunks (arguments arrive in multiple deltas)
    - Multiple tool_calls with different indices
    - Content block transitions (text -> tool_use -> text -> tool_use)
    """

    async def event_generator() -> AsyncGenerator[str, None]:
        # State machine variables
        first_chunk = True
        output_tokens = 0
        input_tokens = 0
        msg_id = f"msg_{uuid.uuid4().hex[:24]}"
        stop_reason = "end_turn"
        pending_text = False

        # Tool call state: keyed by OpenAI index
        # Each: {id, name, partial_json, emitted_start}
        tool_states: dict[int, dict[str, Any]] = {}

        def _sse_event(event_type: str, data: dict[str, Any]) -> str:
            """Format an SSE event string."""
            return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"

        try:
            async with upstream.stream("POST", "/v1/chat/completions", json=payload) as resp:
                if resp.status_code != 200:
                    logger.warning("Upstream streaming returned HTTP %d", resp.status_code)
                    msg_start_data = {
                        "type": "message_start",
                        "message": {
                            "id": "msg_error",
                            "type": "message",
                            "role": "assistant",
                            "content": [],
                            "model": model,
                            "stop_reason": None,
                            "stop_sequence": None,
                            "usage": {"input_tokens": 0, "output_tokens": 0},
                        },
                    }
                    yield _sse_event("message_start", msg_start_data)
                    error_data = {
                        "type": "error",
                        "error": {
                            "type": "api_error",
                            "message": f"Upstream returned HTTP {resp.status_code}",
                        },
                    }
                    yield _sse_event("error", error_data)
                    yield "event: message_stop\ndata: {}\n\n"
                    return

                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data == "[DONE]":
                        # End of stream: stop all pending blocks
                        if pending_text:
                            pending_text = False
                            yield _sse_event(
                                "content_block_stop",
                                {"type": "content_block_stop", "index": 0},
                            )
                        for tc_idx in list(tool_states.keys()):
                            if tool_states[tc_idx].get("emitted_start"):
                                yield _sse_event(
                                    "content_block_stop",
                                    {"type": "content_block_stop", "index": tc_idx + 1},
                                )
                                tool_states[tc_idx]["emitted_start"] = False

                        # Final message events
                        yield _sse_event(
                            "message_delta",
                            {
                                "type": "message_delta",
                                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                                "usage": {"output_tokens": output_tokens},
                            },
                        )
                        yield "event: message_stop\ndata: {}\n\n"
                        return

                    try:
                        chunk: dict[str, Any] = json.loads(data)
                    except json.JSONDecodeError:
                        logger.warning("Failed to parse SSE chunk: %s", data[:100])
                        continue

                    choices: list[dict[str, Any]] = chunk.get("choices") or []
                    if not choices:
                        usage: dict[str, Any] = chunk.get("usage") or {}
                        if usage:
                            input_tokens = int(usage.get("prompt_tokens") or input_tokens)
                            output_tokens = int(usage.get("completion_tokens") or output_tokens)
                        continue

                    choice: dict[str, Any] = choices[0]
                    delta: dict[str, Any] = choice.get("delta") or {}
                    finish: str | None = choice.get("finish_reason")
                    tc_list: list[dict[str, Any]] | None = delta.get("tool_calls")
                    text: str | None = delta.get("content")

                    # Emit message_start on first chunk
                    if first_chunk:
                        yield _sse_event(
                            "message_start",
                            {
                                "message": {
                                    "id": msg_id,
                                    "type": "message",
                                    "role": "assistant",
                                    "content": [],
                                    "model": model,
                                    "stop_reason": None,
                                    "stop_sequence": None,
                                    "usage": {"input_tokens": 0, "output_tokens": 0},
                                },
                            },
                        )
                        first_chunk = False

                    # --- Process tool calls ---
                    if tc_list:
                        # Stop any pending text before starting tool_use
                        if pending_text:
                            pending_text = False
                            yield _sse_event(
                                "content_block_stop",
                                {"type": "content_block_stop", "index": 0},
                            )

                        for i, tc in enumerate(tc_list):
                            tc_idx = tc.get("index", i)
                            fn = tc.get("function", {})

                            if tc_idx not in tool_states:
                                tool_states[tc_idx] = {
                                    "id": "",
                                    "name": "",
                                    "partial_json": "",
                                    "emitted_start": False,
                                }
                            ts = tool_states[tc_idx]

                            # Emit tool_use content_block_start on first arrival
                            if not ts["emitted_start"]:
                                if tc.get("id"):
                                    ts["id"] = tc["id"]
                                if fn.get("name"):
                                    ts["name"] = fn["name"]
                                yield _sse_event(
                                    "content_block_start",
                                    {
                                        "type": "content_block_start",
                                        "index": tc_idx + 1,
                                        "content_block": {
                                            "type": "tool_use",
                                            "id": ts["id"] or f"toolu_{uuid.uuid4().hex[:24]}",
                                            "name": ts["name"],
                                        },
                                    },
                                )
                                ts["emitted_start"] = True

                            # Accumulate partial JSON arguments and emit delta
                            if fn.get("arguments"):
                                ts["partial_json"] += fn["arguments"]
                                yield _sse_event(
                                    "content_block_delta",
                                    {
                                        "type": "content_block_delta",
                                        "index": tc_idx + 1,
                                        "delta": {
                                            "type": "input_json_delta",
                                            "partial_json": fn["arguments"],
                                        },
                                    },
                                )

                    # --- Process text content ---
                    if text:
                        # Stop any pending tool_use before text
                        for tc_idx in list(tool_states.keys()):
                            if tool_states[tc_idx].get("emitted_start"):
                                yield _sse_event(
                                    "content_block_stop",
                                    {"type": "content_block_stop", "index": tc_idx + 1},
                                )
                                tool_states[tc_idx]["emitted_start"] = False

                        # Open the text block before its first delta — the Anthropic
                        # streaming protocol requires a content_block_start for the
                        # index before any content_block_delta. pending_text (already
                        # used to emit the matching content_block_stop) ensures we
                        # open it exactly once per text run.
                        if not pending_text:
                            yield _sse_event(
                                "content_block_start",
                                {
                                    "type": "content_block_start",
                                    "index": 0,
                                    "content_block": {"type": "text", "text": ""},
                                },
                            )

                        yield _sse_event(
                            "content_block_delta",
                            {
                                "type": "content_block_delta",
                                "index": 0,
                                "delta": {"type": "text_delta", "text": text},
                            },
                        )
                        pending_text = True

                    # --- Handle finish_reason ---
                    if finish:
                        # Stop all pending blocks
                        if pending_text:
                            pending_text = False
                            yield _sse_event(
                                "content_block_stop",
                                {"type": "content_block_stop", "index": 0},
                            )
                        for tc_idx in list(tool_states.keys()):
                            if tool_states[tc_idx].get("emitted_start"):
                                yield _sse_event(
                                    "content_block_stop",
                                    {"type": "content_block_stop", "index": tc_idx + 1},
                                )
                                tool_states[tc_idx]["emitted_start"] = False

                        stop_reason = "end_turn" if finish == "stop" else "max_tokens"
                        if finish == "tool_calls":
                            stop_reason = "tool_use"

                    # --- Handle usage chunks ---
                    usage = chunk.get("usage") or {}
                    if usage:
                        input_tokens = int(usage.get("prompt_tokens") or input_tokens)
                        output_tokens = int(usage.get("completion_tokens") or output_tokens)

        except httpx.HTTPStatusError as exc:
            logger.warning("Upstream streaming HTTP status error: %s", exc)
            for event in make_http_error_sse(
                exc.response.status_code, f"Upstream HTTP error: {exc}", model
            ):
                yield event
        except httpx.ConnectError as exc:
            logger.warning("Upstream streaming connection failed: %s", exc)
            for event in make_network_error_sse(exc, model):
                yield event
        except httpx.TimeoutException as exc:
            logger.warning("Upstream streaming timed out: %s", exc)
            for event in make_network_error_sse(exc, model):
                yield event
        except httpx.RequestError as exc:
            logger.warning("Upstream streaming request error: %s", exc)
            for event in make_network_error_sse(exc, model):
                yield event
        except httpx.HTTPError as exc:
            logger.warning("Upstream streaming HTTP error: %s", exc)
            for event in make_http_error_sse(500, f"Upstream HTTP error: {exc}", model):
                yield event
        except Exception as exc:
            logger.warning("Upstream streaming unexpected error: %s", exc, exc_info=True)
            for event in make_http_error_sse(500, f"Upstream error: {exc}", model):
                yield event

    return StreamingResponse(
        content=event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------


@router.post("/v1/messages", response_model=None)
async def proxy_anthropic_messages(
    request: AnthropicRequest,
    _http_request: Request,
    upstream: httpx.AsyncClient | None = Depends(get_upstream_client),
    settings: Any = Depends(get_settings_for_proxy),
) -> JSONResponse | StreamingResponse:
    """Proxy Anthropic Messages API requests through the AgentAlloy pipeline.

    1. Convert Anthropic request → OpenAI ProxyRequest
    2. Build upstream payload (with model resolution)
    3. Forward to upstream and convert response back to Anthropic format
    """
    if upstream is None:
        return _upstream_not_configured_error()

    openai_request = _anthropic_to_openai(request)
    try:
        payload = _build_payload(openai_request, settings.upstream_model)
    except ValueError as e:
        return JSONResponse(
            status_code=503,
            content={
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": str(e),
                },
            },
        )

    if request.stream:
        return _stream_anthropic_response(upstream, payload, request.model)

    # Non-streaming
    try:
        resp = await upstream.post("/v1/chat/completions", json=payload)
    except httpx.ConnectError as e:
        logger.warning("Upstream connection failed: %s", e)
        return JSONResponse(
            status_code=503,
            content={
                "type": "error",
                "error": {"type": "overloaded_error", "message": f"Upstream unavailable: {e}"},
            },
        )
    except httpx.TimeoutException as e:
        logger.warning("Upstream timeout: %s", e)
        return JSONResponse(
            status_code=503,
            content={
                "type": "error",
                "error": {"type": "api_error", "message": f"Upstream timeout: {e}"},
            },
        )
    except httpx.RequestError as e:
        logger.warning("Upstream request error: %s", e)
        return JSONResponse(
            status_code=503,
            content={
                "type": "error",
                "error": {"type": "api_error", "message": f"Upstream request error: {e}"},
            },
        )
    except httpx.HTTPError as e:
        logger.warning("Upstream HTTP error: %s", e)
        return JSONResponse(
            status_code=503,
            content={
                "type": "error",
                "error": {"type": "api_error", "message": f"Upstream HTTP error: {e}"},
            },
        )
    except Exception as e:
        logger.warning("Upstream unexpected error: %s", e, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "type": "error",
                "error": {"type": "api_error", "message": f"Upstream error: {e}"},
            },
        )

    if resp.status_code != 200:
        return JSONResponse(
            status_code=resp.status_code,
            content={"type": "error", "error": {"type": "api_error", "message": resp.text}},
        )

    openai_body: dict[str, Any] = resp.json()
    anthropic_body = _openai_to_anthropic(openai_body, request.model)
    return JSONResponse(content=anthropic_body)
