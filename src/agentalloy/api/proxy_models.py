"""Pydantic models for the OpenAI-compatible proxy endpoint.

Mirrors the OpenAI chat completion request/response format so the proxy can
parse incoming requests and shape responses without depending on the ``openai``
SDK.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel


class ProxyMessage(BaseModel):
    """A single chat message (system / user / assistant / tool)."""

    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[dict[str, Any]] | None = None

    # Tool-use fields
    tool_calls: list[dict[str, Any]] | None = None
    tool_call: dict[str, Any] | None = None
    tool: dict[str, Any] | None = None
    # Correlates a role="tool" result message with the assistant tool_call it
    # answers (OpenAI requires it on tool messages).
    tool_call_id: str | None = None


class ProxyRequest(BaseModel):
    """Input to POST /v1/chat/completions (OpenAI-compatible shape)."""

    model: str
    messages: list[ProxyMessage]
    stream: bool = False

    # Optional OpenAI parameters
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    n: int | None = None
    user: str | None = None

    # Harness-specific metadata (e.g. working directory)
    metadata: dict[str, Any] | None = None

    # Tool-use support
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None


class ProxyChoice(BaseModel):
    """A single choice in a chat completion response."""

    index: int
    message: ProxyMessage
    finish_reason: Literal["stop", "length", "content_filter"] | None = None


class ProxyStreamDelta(BaseModel):
    """A partial chunk in a streaming response."""

    role: Literal["assistant"] | None = None
    content: str | None = None

    # Tool-use support
    tool_calls: list[dict[str, Any]] | None = None


class ProxyStreamChunk(BaseModel):
    """An SSE chunk for streaming chat completions."""

    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int
    model: str
    choices: list[dict[str, Any]]

    def set_delta(
        self, index: int, delta: ProxyStreamDelta, *, finish_reason: str | None = None
    ) -> None:
        """Set or update the delta for a given choice index.

        Creates the entry if it does not exist.
        """
        while len(self.choices) <= index:
            self.choices.append({"index": index, "delta": {}})
        entry = self.choices[index]
        entry["delta"] = delta.model_dump(exclude_none=True)
        if finish_reason is not None:
            entry["finish_reason"] = finish_reason
        else:
            entry.pop("finish_reason", None)


class ProxyResponseUsage(BaseModel):
    """Token usage metadata for a completion response."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ProxyResponse(BaseModel):
    """Successful chat completion response (non-streaming)."""

    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[ProxyChoice]
    usage: ProxyResponseUsage | None = None

    @staticmethod
    def _now_ts() -> int:
        return int(datetime.now(UTC).timestamp())
