"""Tests for Anthropic Messages API router. Maps to Step 7."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from agentalloy.api.proxy_anthropic_models import (
    AnthropicMessage,
    AnthropicRequest,
    AnthropicTool,
    AnthropicToolChoice,
)
from agentalloy.api.proxy_anthropic_router import (
    _anthropic_message_to_openai,
    _anthropic_to_openai,
    _anthropic_tool_choice_to_openai,
    _anthropic_tool_to_openai,
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

    def test_tool_use_preserved_in_streaming(self) -> None:
        """tool_calls in streaming delta are preserved as tool_use content blocks."""
        chunks: list[dict[str, Any]] = [
            {
                "id": "chatcmpl-1",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "tc1",
                                    "function": {"name": "search", "arguments": ""},
                                }
                            ],
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
        text_deltas = [
            e["delta"]["text"]
            for e in events
            if e.get("type") == "content_block_delta"
            and e.get("delta", {}).get("type") == "text_delta"
        ]
        combined = "".join(text_deltas)
        assert "prefix" in combined
        assert " text" in combined
        # Tool_use blocks should be present
        tc_starts = [
            e
            for e in events
            if e.get("type") == "content_block_start"
            and e.get("content_block", {}).get("type") == "tool_use"
        ]
        assert len(tc_starts) >= 1
        assert tc_starts[0]["content_block"]["name"] == "search"

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


# ---------------------------------------------------------------------------
# TestAnthropicToolToOpenAI
# ---------------------------------------------------------------------------


class TestAnthropicToolToOpenAI:
    """Unit tests for _anthropic_tool_to_openai()."""

    def test_basic_tool_conversion(self) -> None:
        """A simple Anthropic tool converts to OpenAI function format."""
        tool = AnthropicTool(
            name="search",
            description="Search the web",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        )
        result = _anthropic_tool_to_openai(tool.model_dump(mode="json"))
        assert result["type"] == "function"
        assert result["function"]["name"] == "search"
        assert result["function"]["description"] == "Search the web"
        assert result["function"]["parameters"]["type"] == "object"
        assert result["function"]["parameters"]["properties"]["query"]["type"] == "string"
        assert result["function"]["parameters"]["required"] == ["query"]

    def test_tool_without_description(self) -> None:
        """A tool without description gets None for description field."""
        tool = AnthropicTool(
            name="echo",
            input_schema={"type": "object", "properties": {"msg": {"type": "string"}}},
        )
        result = _anthropic_tool_to_openai(tool.model_dump(mode="json"))
        assert result["function"]["name"] == "echo"
        assert result["function"]["description"] is None

    def test_tool_empty_input_schema(self) -> None:
        """A tool with empty input_schema gets empty dict as parameters."""
        tool = AnthropicTool(
            name="ping",
            input_schema={},
        )
        result = _anthropic_tool_to_openai(tool.model_dump(mode="json"))
        assert result["function"]["parameters"] == {}


# ---------------------------------------------------------------------------
# TestAnthropicToolChoiceToOpenAI
# ---------------------------------------------------------------------------


class TestAnthropicToolChoiceToOpenAI:
    """Unit tests for _anthropic_tool_choice_to_openai()."""

    def test_none_tool_choice(self) -> None:
        """None tool_choice returns None."""
        assert _anthropic_tool_choice_to_openai(None) is None

    def test_auto_type(self) -> None:
        """Anthropic auto maps to OpenAI auto."""
        result = _anthropic_tool_choice_to_openai({"type": "auto"})
        assert result == "auto"

    def test_any_type(self) -> None:
        """Anthropic any maps to OpenAI required."""
        result = _anthropic_tool_choice_to_openai({"type": "any"})
        assert result == "required"

    def test_tool_type_with_name(self) -> None:
        """Anthropic tool with name maps to OpenAI function selector."""
        result = _anthropic_tool_choice_to_openai({"type": "tool", "name": "search"})
        assert result == {"type": "function", "function": {"name": "search"}}

    def test_tool_type_without_name(self) -> None:
        """Anthropic tool without name gets None in function field."""
        result = _anthropic_tool_choice_to_openai({"type": "tool"})
        assert result == {"type": "function", "function": {"name": None}}


# ---------------------------------------------------------------------------
# TestAnthropicMessageToOpenAI
# ---------------------------------------------------------------------------


class TestAnthropicMessageToOpenAI:
    """Unit tests for _anthropic_message_to_openai()."""

    def test_user_text_message(self) -> None:
        """Simple user text message passes through unchanged."""
        msg = _anthropic_message_to_openai({"role": "user", "content": "Hello"})
        assert msg.role == "user"
        assert msg.content == "Hello"
        assert msg.tool_calls is None

    def test_assistant_text_message(self) -> None:
        """Simple assistant text message passes through unchanged."""
        msg = _anthropic_message_to_openai({"role": "assistant", "content": "Hi there"})
        assert msg.role == "assistant"
        assert msg.content == "Hi there"
        assert msg.tool_calls is None

    def test_tool_result_role(self) -> None:
        """Anthropic tool role maps to OpenAI tool role."""
        msg = _anthropic_message_to_openai(
            {
                "role": "tool",
                "content": "Result data",
            }
        )
        assert msg.role == "tool"
        assert msg.content == "Result data"

    def test_assistant_with_tool_calls(self) -> None:
        """Assistant message with tool_calls gets OpenAI tool_calls format."""
        msg = _anthropic_message_to_openai(
            {
                "role": "assistant",
                "content": "Let me search for that.",
                "tool_calls": [
                    {
                        "id": "toolu_123",
                        "name": "search",
                        "input": {"query": "openai api"},
                    }
                ],
            }
        )
        assert msg.role == "assistant"
        assert msg.content == "Let me search for that."
        assert msg.tool_calls is not None
        assert len(msg.tool_calls) == 1
        assert msg.tool_calls[0]["id"] == "toolu_123"
        assert msg.tool_calls[0]["type"] == "function"
        assert msg.tool_calls[0]["function"]["name"] == "search"
        assert msg.tool_calls[0]["function"]["arguments"] == '{"query": "openai api"}'

    def test_assistant_with_multiple_tool_calls(self) -> None:
        """Assistant message with multiple tool_calls."""
        msg = _anthropic_message_to_openai(
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "toolu_1",
                        "name": "search",
                        "input": {"q": "first"},
                    },
                    {
                        "id": "toolu_2",
                        "name": "browse",
                        "input": {"url": "https://example.com"},
                    },
                ],
            }
        )
        assert msg.tool_calls is not None
        assert len(msg.tool_calls) == 2
        assert msg.tool_calls[0]["function"]["name"] == "search"
        assert msg.tool_calls[1]["function"]["name"] == "browse"

    def test_assistant_tool_calls_no_id_gets_generated(self) -> None:
        """Tool calls without id get a generated call_<uuid>."""
        msg = _anthropic_message_to_openai(
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "name": "calc",
                        "input": {"expr": "2+2"},
                    }
                ],
            }
        )
        assert msg.tool_calls is not None
        assert msg.tool_calls[0]["id"].startswith("call_")
        assert msg.tool_calls[0]["function"]["name"] == "calc"

    def test_content_block_array_with_tool_use(self) -> None:
        """Content-block array with tool_use blocks is passed through."""
        msg = _anthropic_message_to_openai(
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Thinking..."},
                    {"type": "tool_use", "id": "toolu_1", "name": "search", "input": {"q": "test"}},
                ],
            }
        )
        assert msg.role == "assistant"
        assert isinstance(msg.content, list)
        assert msg.content[0]["type"] == "text"
        assert msg.content[1]["type"] == "tool_use"

    def test_content_block_array_text_only(self) -> None:
        """Text-only content-block array is merged into a single string."""
        msg = _anthropic_message_to_openai(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "First part"},
                    {"type": "text", "text": " Second part"},
                ],
            }
        )
        assert msg.role == "user"
        assert msg.content == "First part Second part"


# ---------------------------------------------------------------------------
# TestAnthropicToOpenAIWithTools
# ---------------------------------------------------------------------------


class TestAnthropicToOpenAIWithTools:
    """Unit tests for _anthropic_to_openai() with tools, tool_choice, tool_calls."""

    def test_tools_conversion(self) -> None:
        """AnthropicRequest.tools -> ProxyRequest.tools."""
        req = AnthropicRequest(
            model="claude-3-opus",
            max_tokens=1024,
            tools=[
                AnthropicTool(
                    name="search",
                    description="Search the web",
                    input_schema={
                        "type": "object",
                        "properties": {"q": {"type": "string"}},
                        "required": ["q"],
                    },
                ),
                AnthropicTool(
                    name="calc",
                    description="Calculator",
                    input_schema={"type": "object", "properties": {"expr": {"type": "string"}}},
                ),
            ],
            messages=[AnthropicMessage(role="user", content="What's the weather?")],
        )
        openai_req = _anthropic_to_openai(req)
        assert openai_req.tools is not None
        assert len(openai_req.tools) == 2
        assert openai_req.tools[0]["type"] == "function"
        assert openai_req.tools[0]["function"]["name"] == "search"
        assert openai_req.tools[1]["function"]["name"] == "calc"

    def test_no_tools(self) -> None:
        """When tools is None, ProxyRequest.tools is None."""
        req = AnthropicRequest(
            model="claude-3-opus",
            max_tokens=1024,
            messages=[AnthropicMessage(role="user", content="Hello")],
        )
        openai_req = _anthropic_to_openai(req)
        assert openai_req.tools is None

    def test_tool_choice_auto(self) -> None:
        """Anthropic tool_choice auto -> OpenAI tool_choice 'auto'."""
        req = AnthropicRequest(
            model="claude-3-opus",
            max_tokens=1024,
            tool_choice=AnthropicToolChoice(type="auto"),
            messages=[AnthropicMessage(role="user", content="Hello")],
        )
        openai_req = _anthropic_to_openai(req)
        assert openai_req.tool_choice == "auto"

    def test_tool_choice_any(self) -> None:
        """Anthropic tool_choice any -> OpenAI tool_choice 'required'."""
        req = AnthropicRequest(
            model="claude-3-opus",
            max_tokens=1024,
            tool_choice=AnthropicToolChoice(type="any"),
            messages=[AnthropicMessage(role="user", content="Hello")],
        )
        openai_req = _anthropic_to_openai(req)
        assert openai_req.tool_choice == "required"

    def test_tool_choice_specific_tool(self) -> None:
        """Anthropic tool_choice tool -> OpenAI tool_choice function selector."""
        req = AnthropicRequest(
            model="claude-3-opus",
            max_tokens=1024,
            tool_choice=AnthropicToolChoice(type="tool", name="search"),
            messages=[AnthropicMessage(role="user", content="Hello")],
        )
        openai_req = _anthropic_to_openai(req)
        assert openai_req.tool_choice == {"type": "function", "function": {"name": "search"}}

    def test_tool_choice_none(self) -> None:
        """When tool_choice is None, ProxyRequest.tool_choice is None."""
        req = AnthropicRequest(
            model="claude-3-opus",
            max_tokens=1024,
            messages=[AnthropicMessage(role="user", content="Hello")],
        )
        openai_req = _anthropic_to_openai(req)
        assert openai_req.tool_choice is None

    def test_messages_with_tool_calls(self) -> None:
        """Anthropic messages with tool_calls -> ProxyMessage.tool_calls."""
        req = AnthropicRequest(
            model="claude-3-opus",
            max_tokens=1024,
            messages=[
                AnthropicMessage(role="user", content="Search for openai"),
                AnthropicMessage(
                    role="assistant",
                    content="",
                    tool_calls=[
                        {
                            "id": "toolu_abc123",
                            "name": "search",
                            "input": {"query": "openai"},
                        }
                    ],
                ),
                AnthropicMessage(role="tool", content="Results: ..."),
            ],
        )
        openai_req = _anthropic_to_openai(req)
        assert len(openai_req.messages) == 3
        # User message
        assert openai_req.messages[0].role == "user"
        assert openai_req.messages[0].content == "Search for openai"
        # Assistant with tool_calls
        assert openai_req.messages[1].role == "assistant"
        assert openai_req.messages[1].tool_calls is not None
        assert len(openai_req.messages[1].tool_calls) == 1
        assert openai_req.messages[1].tool_calls[0]["id"] == "toolu_abc123"
        assert openai_req.messages[1].tool_calls[0]["function"]["name"] == "search"
        assert json.loads(openai_req.messages[1].tool_calls[0]["function"]["arguments"]) == {
            "query": "openai"
        }
        # Tool result
        assert openai_req.messages[2].role == "tool"
        assert openai_req.messages[2].content == "Results: ..."

    def test_full_request_with_tools_tool_choice_and_tool_calls(self) -> None:
        """Full Anthropic request with tools, tool_choice, and tool_calls."""
        req = AnthropicRequest(
            model="claude-3-opus",
            max_tokens=1024,
            system="You are a helpful assistant.",
            tools=[
                AnthropicTool(
                    name="weather",
                    description="Get weather info",
                    input_schema={
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                    },
                ),
            ],
            tool_choice=AnthropicToolChoice(type="auto"),
            messages=[
                AnthropicMessage(role="user", content="What's the weather in Paris?"),
                AnthropicMessage(
                    role="assistant",
                    content="",
                    tool_calls=[
                        {
                            "id": "toolu_weather1",
                            "name": "weather",
                            "input": {"city": "Paris"},
                        }
                    ],
                ),
                AnthropicMessage(role="tool", content="Sunny, 22°C"),
                AnthropicMessage(role="user", content="Thanks!"),
            ],
        )
        openai_req = _anthropic_to_openai(req)

        # System message prepended
        assert openai_req.messages[0].role == "system"
        assert openai_req.messages[0].content == "You are a helpful assistant."

        # User message
        assert openai_req.messages[1].role == "user"
        assert openai_req.messages[1].content == "What's the weather in Paris?"

        # Assistant with tool_calls
        assert openai_req.messages[2].role == "assistant"
        assert openai_req.messages[2].tool_calls is not None
        assert len(openai_req.messages[2].tool_calls) == 1
        assert openai_req.messages[2].tool_calls[0]["function"]["name"] == "weather"

        # Tool result
        assert openai_req.messages[3].role == "tool"
        assert openai_req.messages[3].content == "Sunny, 22°C"

        # Final user message
        assert openai_req.messages[4].role == "user"
        assert openai_req.messages[4].content == "Thanks!"

        # Tools converted
        assert openai_req.tools is not None
        assert len(openai_req.tools) == 1
        assert openai_req.tools[0]["function"]["name"] == "weather"

        # Tool choice converted
        assert openai_req.tool_choice == "auto"

        # Other fields passed through
        assert openai_req.model == "claude-3-opus"
        assert openai_req.max_tokens == 1024

    def test_openai_request_preserves_temperature_and_top_p(self) -> None:
        """Temperature and top_p are passed through."""
        req = AnthropicRequest(
            model="claude-3-opus",
            max_tokens=1024,
            temperature=0.7,
            top_p=0.9,
            messages=[AnthropicMessage(role="user", content="Hello")],
        )
        openai_req = _anthropic_to_openai(req)
        assert openai_req.temperature == 0.7
        assert openai_req.top_p == 0.9

    def test_openai_request_preserves_stream(self) -> None:
        """Stream flag is passed through."""
        req = AnthropicRequest(
            model="claude-3-opus",
            max_tokens=1024,
            stream=True,
            messages=[AnthropicMessage(role="user", content="Hello")],
        )
        openai_req = _anthropic_to_openai(req)
        assert openai_req.stream is True
