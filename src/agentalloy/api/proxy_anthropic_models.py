"""Pydantic models for the Anthropic Messages API proxy endpoint.

Mirrors the Anthropic /v1/messages request/response format (text-only subset).
Tool use and vision content blocks are explicitly out of scope for Phase 1.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


class AnthropicMessage(BaseModel):
    """A single message in an Anthropic conversation."""

    role: Literal["user", "assistant"]
    content: str  # Text only — content-block arrays not supported in Phase 1


class AnthropicRequest(BaseModel):
    """Input to POST /v1/messages (Anthropic Messages API shape)."""

    model: str
    max_tokens: int
    system: str | None = None
    messages: list[AnthropicMessage]
    stream: bool = False
    temperature: float | None = None
    top_p: float | None = None


class AnthropicContentBlock(BaseModel):
    """A single text content block in an Anthropic response."""

    type: Literal["text"] = "text"
    text: str


class AnthropicResponse(BaseModel):
    """Successful non-streaming response from /v1/messages."""

    id: str
    type: Literal["message"] = "message"
    role: Literal["assistant"] = "assistant"
    content: list[AnthropicContentBlock]
    model: str
    stop_reason: Literal["end_turn", "max_tokens"] | None = None
    stop_sequence: str | None = None
    usage: dict[str, Any]  # input_tokens, output_tokens
