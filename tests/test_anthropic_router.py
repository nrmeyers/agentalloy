"""Tests for Anthropic Messages API router. Maps to Step 7."""

from __future__ import annotations

import time
import uuid
from typing import Any

from agentalloy.api.proxy_anthropic_models import (
    AnthropicMessage,
    AnthropicRequest,
)
from agentalloy.api.proxy_anthropic_router import (
    _anthropic_to_openai,
    _openai_stream_to_anthropic,
    _openai_to_anthropic,
    _stream_anthropic_response,
)

# ---------------------------------------------------------------------------
# TestAnthropicToOpenAI
# ---------------------------------------------------------------------------


class TestAnthropicToOpenAI:
    """Unit tests for _anthropic_to_openai() translation."""

    def test_system_string_to_message(self) -> None:
        """system string is prepended as a system role message."""
        req = AnthropicRequest(
            model="claude-3-opus",
            max_tokens=1024,
            system="You are a helpful assistant.",
            messages=[AnthropicMessage(role="user", content="Hello")],
        )
        openai_req = _anthropic_to_openai(req)
        assert openai_req.messages[0].role == "system"
        assert openai_req.messages[0].content == "You are a helpful assistant."
        assert openai_req.messages[1].role == "user"
        assert openai_req.messages[1].content == "Hello"

    def test_messages_passthrough(self) -> None:
        """messages are passed through with role and content intact."""
        req = AnthropicRequest(
            model="claude-3-opus",
            max_tokens=1024,
            messages=[
                AnthropicMessage(role="user", content="What is 2+2?"),
                AnthropicMessage(role="assistant", content="4"),
                AnthropicMessage(role="user", content="Thanks"),
            ],
        )
        openai_req = _anthropic_to_openai(req)
        # No system message prepended
        assert len(openai_req.messages) == 3
        assert openai_req.messages[0].role == "user"
        assert openai_req.messages[1].role == "assistant"

    def test_no_system_gives_no_system_message(self) -> None:
        """When system is None, no system message is prepended."""
        req = AnthropicRequest(
            model="claude-3-opus",
            max_tokens=1024,
            messages=[AnthropicMessage(role="user", content="Hi")],
        )
        openai_req = _anthropic_to_openai(req)
        assert len(openai_req.messages) == 1
        assert openai_req.messages[0].role == "user"


# ---------------------------------------------------------------------------
# TestOpenAItoAnthropic
# ---------------------------------------------------------------------------


class TestOpenAItoAnthropic:
    """Unit tests for _openai_to_anthropic() translation."""

    def _make_openai_response(self, text: str = "Hello!", finish: str = "stop") -> dict[str, Any]:
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "gpt-4o",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": finish,
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

    def test_response_translation(self) -> None:
        """OpenAI response is correctly translated to Anthropic format."""
        body = self._make_openai_response("The answer is 42.")
        result = _openai_to_anthropic(body, "claude-3-opus")
        assert result["type"] == "message"
        assert result["role"] == "assistant"
        assert len(result["content"]) == 1
        assert result["content"][0]["type"] == "text"
        assert result["content"][0]["text"] == "The answer is 42."

    def test_usage_translation(self) -> None:
        """Token usage is translated from OpenAI to Anthropic field names."""
        body = self._make_openai_response()
        result = _openai_to_anthropic(body, "claude-3-opus")
        assert result["usage"]["input_tokens"] == 10
        assert result["usage"]["output_tokens"] == 5

    def test_stop_reason_translation(self) -> None:
        """finish_reason='stop' maps to stop_reason='end_turn'."""
        body = self._make_openai_response(finish="stop")
        result = _openai_to_anthropic(body, "claude-3-opus")
        assert result["stop_reason"] == "end_turn"

    def test_tool_use_stripped_in_streaming(self) -> None:
        """tool_calls in streaming delta are stripped; only text passes through."""
        chunks: list[dict[str, Any]] = [
            {
                "id": "chatcmpl-1",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "tool_calls": [{"id": "tc1", "function": {"name": "search"}}],
                            "content": "prefix",
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "chatcmpl-1",
                "choices": [{"index": 0, "delta": {"content": " text"}, "finish_reason": "stop"}],
            },
        ]
        events = _openai_stream_to_anthropic(chunks, "claude-3-opus")
        # Collect text deltas
        text_deltas = [e["delta"]["text"] for e in events if e.get("type") == "content_block_delta"]
        combined = "".join(text_deltas)
        assert "prefix" in combined
        assert " text" in combined
        # No tool_use blocks in events
        types = [e.get("type") for e in events]
        assert "tool_use" not in types

    def test_streaming_usage_in_delta_not_stop(self) -> None:
        """Usage tokens appear in message_delta, not message_stop."""
        chunks: list[dict[str, Any]] = [
            {
                "id": "chatcmpl-1",
                "choices": [{"index": 0, "delta": {"content": "hello"}, "finish_reason": None}],
            },
            {
                "id": "chatcmpl-1",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11},
            },
        ]
        events = _openai_stream_to_anthropic(chunks, "claude-3-opus")
        delta_event = next(e for e in events if e.get("type") == "message_delta")
        stop_event = next(e for e in events if e.get("type") == "message_stop")
        assert "usage" in delta_event
        assert delta_event["usage"]["output_tokens"] == 3
        assert "usage" not in stop_event


# ---------------------------------------------------------------------------
# TestAnthropicProxyIntegration
# ---------------------------------------------------------------------------


class TestAnthropicProxyIntegration:
    """Integration test for the full Anthropic request-response cycle."""

    def test_full_request_response_cycle(self) -> None:
        """_anthropic_to_openai and _openai_to_anthropic are inverse operations."""
        req = AnthropicRequest(
            model="claude-3-opus",
            max_tokens=100,
            system="Be concise.",
            messages=[AnthropicMessage(role="user", content="What year is it?")],
        )
        _openai_req = _anthropic_to_openai(req)

        # Simulate upstream response
        fake_response: dict[str, Any] = {
            "id": "chatcmpl-abc",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "gpt-4o",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "It is 2024."},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 15, "completion_tokens": 6, "total_tokens": 21},
        }
        anthropic_response = _openai_to_anthropic(fake_response, req.model)

        # Verify round-trip
        assert anthropic_response["type"] == "message"
        assert anthropic_response["content"][0]["text"] == "It is 2024."
        assert anthropic_response["stop_reason"] == "end_turn"
        assert anthropic_response["usage"]["input_tokens"] == 15
        assert anthropic_response["usage"]["output_tokens"] == 6


class TestStreamAnthropicResponse:
    """Tests for _stream_anthropic_response streaming translation."""

    def test_stream_anthropic_response_produces_events(self) -> None:
        """Streaming response produces Anthropic SSE events."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        # Mock upstream response with OpenAI SSE chunks
        mock_response = MagicMock()
        mock_response.status_code = 200

        # aiter_lines must be an async iterator
        async def async_lines():
            for line in [
                'data: {"id":"chatcmpl-1","choices":[{"index":0,"delta":{"role":"assistant","content":"Hello"},"finish_reason":null}]}',
                'data: {"id":"chatcmpl-1","choices":[{"index":0,"delta":{"content":" world"},"finish_reason":"stop"}]}',
                'data: {"usage":{"prompt_tokens":5,"completion_tokens":2}}',
                "data: [DONE]",
            ]:
                yield line

        mock_response.aiter_lines = MagicMock(return_value=async_lines())

        mock_client = MagicMock()
        mock_client.stream = MagicMock()
        mock_client.stream.return_value.__aenter__ = AsyncMock(return_value=mock_response)
        mock_client.stream.return_value.__aexit__ = AsyncMock(return_value=None)

        response = _stream_anthropic_response(
            mock_client,  # type: ignore[arg-type]
            {"model": "claude-3-opus", "messages": []},
            "claude-3-opus",
        )

        # Collect all yielded lines
        collected: list[str] = []

        async def collect() -> None:
            async for item in response.body_iterator:
                collected.append(str(item))

        asyncio.get_event_loop().run_until_complete(collect())

        # Check that Anthropic SSE events are produced
        joined = "".join(collected)
        assert "event: message_start" in joined
        assert "event: content_block_delta" in joined
        assert "event: message_delta" in joined
        assert "event: message_stop" in joined
        # Check that text is in the output
        assert "Hello" in joined
        assert "world" in joined
