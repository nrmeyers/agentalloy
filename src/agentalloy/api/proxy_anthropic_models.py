"""Pydantic models for the Anthropic Messages API proxy endpoint.

Mirrors the Anthropic /v1/messages request/response format with support for
tool use (function calling).  Text-only mode is still supported when no
tools are provided.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Anthropic tool definitions
# ---------------------------------------------------------------------------


class AnthropicToolInputSchema(BaseModel):
    """The JSON schema for an Anthropic tool's input."""

    type: Literal["object"] = "object"
    properties: dict[str, Any] | None = None
    required: list[str] | None = None


class AnthropicTool(BaseModel):
    """A tool definition in Anthropic format.

    Mirrors Anthropic's ``tool_use`` tool block:
    { name, description, input_schema }
    """

    name: str
    description: str | None = None
    input_schema: dict[str, Any]


# ---------------------------------------------------------------------------
# Anthropic tool_choice
# ---------------------------------------------------------------------------


class AnthropicToolChoiceModel(BaseModel):
    """Disambiguated tool_choice: {type: 'tool', name: str}."""

    type: Literal["tool"]
    name: str


class AnthropicToolChoice(BaseModel):
    """Tool choice in Anthropic format.

    Accepts:
    - ``None`` / missing → no preference (proxy passes null)
    - ``{"type": "auto"}``
    - ``{"type": "any"}``
    - ``{"type": "tool", "name": "..."}``
    """

    type: Literal["auto", "any", "tool"]
    name: str | None = None  # present when type == "tool"


# ---------------------------------------------------------------------------
# Anthropic message content blocks
# ---------------------------------------------------------------------------


class AnthropicTextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str


class AnthropicToolUseBlock(BaseModel):
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any]


class AnthropicToolResultBlock(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str


class AnthropicImageBlock(BaseModel):
    """Inline image content block (source sub-object)."""

    type: Literal["image"] = "image"
    source: dict[str, Any]  # {type, data, media_type}


class AnthropicContentBlock(BaseModel):
    """A single content block in an Anthropic message.

    Union of text, tool_use, tool_result, and image blocks.
    """

    type: Literal["text", "tool_use", "tool_result", "image"]
    text: str | None = None
    id: str | None = None
    name: str | None = None
    input: dict[str, Any] | None = None
    tool_use_id: str | None = None
    content: str | None = None
    source: dict[str, Any] | None = None


class AnthropicMessage(BaseModel):
    """A single message in an Anthropic conversation.

    Supports both text-only (``content: str``) and content-block-array
    (``content: list[AnthropicContentBlock]``) formats.  When tools are
    enabled, assistant messages may carry ``tool_calls`` (tool_use blocks)
    and user messages may carry ``tool_result`` blocks.
    """

    role: Literal["user", "assistant", "tool"]
    content: str | list[AnthropicContentBlock]

    # Legacy convenience: tool_calls carried on the message level for
    # assistant messages that used tool-use blocks.
    tool_calls: list[dict[str, Any]] | None = None


# ---------------------------------------------------------------------------
# Anthropic request
# ---------------------------------------------------------------------------


class AnthropicRequest(BaseModel):
    """Input to POST /v1/messages (Anthropic Messages API shape)."""

    model: str
    max_tokens: int
    system: str | list[AnthropicContentBlock] | None = None
    messages: list[AnthropicMessage]
    stream: bool = False
    temperature: float | None = None
    top_p: float | None = None

    # Tool-use fields
    tools: list[AnthropicTool] | None = None
    tool_choice: AnthropicToolChoice | None = None


# ---------------------------------------------------------------------------
# Anthropic response
# ---------------------------------------------------------------------------


class AnthropicResponse(BaseModel):
    """Successful non-streaming response from /v1/messages."""

    id: str
    type: Literal["message"] = "message"
    role: Literal["assistant"] = "assistant"
    content: list[AnthropicContentBlock]
    model: str
    stop_reason: Literal["end_turn", "max_tokens", "tool_use"] | None = None
    stop_sequence: str | None = None
    usage: dict[str, Any]  # input_tokens, output_tokens
