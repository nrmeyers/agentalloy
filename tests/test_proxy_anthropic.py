"""Tests for Anthropic-to-OpenAI translation (Task 6).

Covers:
- AnthropicRequest.tools -> ProxyRequest.tools
- AnthropicRequest.tool_choice -> ProxyRequest.tool_choice
- Anthropic messages with tool_calls -> ProxyMessage.tool_calls
- OpenAI response with tool_calls -> Anthropic format
- Streaming tool-call translation
"""

from __future__ import annotations

import json

from agentalloy.api.proxy_anthropic_models import (
    AnthropicContentBlock,
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
    _openai_stream_to_anthropic_interleaved,
    _openai_to_anthropic,
)
from agentalloy.api.proxy_models import ProxyMessage, ProxyRequest

# ---------------------------------------------------------------------------
# AnthropicRequest.tools -> ProxyRequest.tools
# ---------------------------------------------------------------------------


class TestAnthropicToolsToOpenAI:
    """Test that Anthropic tools are translated to OpenAI function-calling format."""

    def test_single_tool_translation(self):
        request = AnthropicRequest(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            messages=[AnthropicMessage(role="user", content="What's the weather?")],
            tools=[
                AnthropicTool(
                    name="get_weather",
                    description="Get the current weather for a location",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "location": {"type": "string", "description": "City name"},
                        },
                        "required": ["location"],
                    },
                )
            ],
        )
        result = _anthropic_to_openai(request)
        assert result.tools is not None
        assert len(result.tools) == 1
        tool = result.tools[0]
        assert tool["type"] == "function"
        assert tool["function"]["name"] == "get_weather"
        assert tool["function"]["description"] == "Get the current weather for a location"
        assert tool["function"]["parameters"]["type"] == "object"
        assert "location" in tool["function"]["parameters"]["properties"]

    def test_multiple_tools_translation(self):
        request = AnthropicRequest(
            model="claude-3-5-sonnet",
            max_tokens=2048,
            messages=[AnthropicMessage(role="user", content="Do stuff")],
            tools=[
                AnthropicTool(name="search", input_schema={"type": "object"}),
                AnthropicTool(name="browse", input_schema={"type": "object"}),
            ],
        )
        result = _anthropic_to_openai(request)
        assert result.tools is not None
        tools = result.tools
        assert len(tools) == 2
        assert tools[0]["function"]["name"] == "search"
        assert tools[1]["function"]["name"] == "browse"

    def test_no_tools_passes_through(self):
        request = AnthropicRequest(
            model="claude-3",
            max_tokens=512,
            messages=[AnthropicMessage(role="user", content="Hello")],
        )
        result = _anthropic_to_openai(request)
        assert result.tools is None

    def test_tool_without_description(self):
        request = AnthropicRequest(
            model="claude-3",
            max_tokens=512,
            messages=[AnthropicMessage(role="user", content="Test")],
            tools=[AnthropicTool(name="noop", input_schema={"type": "object"})],
        )
        result = _anthropic_to_openai(request)
        assert result.tools[0]["function"]["description"] is None


# ---------------------------------------------------------------------------
# AnthropicRequest.tool_choice -> ProxyRequest.tool_choice
# ---------------------------------------------------------------------------


class TestAnthropicToolChoiceToOpenAI:
    """Test tool_choice translation."""

    def test_tool_choice_auto(self):
        request = AnthropicRequest(
            model="claude-3",
            max_tokens=512,
            messages=[AnthropicMessage(role="user", content="Test")],
            tool_choice=AnthropicToolChoice(type="auto"),
        )
        result = _anthropic_to_openai(request)
        assert result.tool_choice == "auto"

    def test_tool_choice_any(self):
        request = AnthropicRequest(
            model="claude-3",
            max_tokens=512,
            messages=[AnthropicMessage(role="user", content="Test")],
            tool_choice=AnthropicToolChoice(type="any"),
        )
        result = _anthropic_to_openai(request)
        assert result.tool_choice == "required"

    def test_tool_choice_named_tool(self):
        request = AnthropicRequest(
            model="claude-3",
            max_tokens=512,
            messages=[AnthropicMessage(role="user", content="Test")],
            tool_choice=AnthropicToolChoice(type="tool", name="get_weather"),
        )
        result = _anthropic_to_openai(request)
        assert result.tool_choice == {
            "type": "function",
            "function": {"name": "get_weather"},
        }

    def test_no_tool_choice(self):
        request = AnthropicRequest(
            model="claude-3",
            max_tokens=512,
            messages=[AnthropicMessage(role="user", content="Test")],
        )
        result = _anthropic_to_openai(request)
        assert result.tool_choice is None

    def test_direct_conversion_auto(self):
        assert _anthropic_tool_choice_to_openai({"type": "auto"}) == "auto"

    def test_direct_conversion_any(self):
        assert _anthropic_tool_choice_to_openai({"type": "any"}) == "required"

    def test_direct_conversion_tool(self):
        result = _anthropic_tool_choice_to_openai({"type": "tool", "name": "foo"})
        assert result == {"type": "function", "function": {"name": "foo"}}

    def test_direct_conversion_none(self):
        assert _anthropic_tool_choice_to_openai(None) is None


# ---------------------------------------------------------------------------
# Anthropic messages with tool_calls -> ProxyMessage.tool_calls
# ---------------------------------------------------------------------------


class TestAnthropicToolCallsToOpenAI:
    """Test that assistant messages with tool_calls are translated."""

    def test_single_tool_call(self):
        msg = AnthropicMessage(
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "toolu_01A09q90qw90lq917835lq9",
                    "name": "get_weather",
                    "input": {"location": "San Francisco"},
                }
            ],
        )
        result = _anthropic_message_to_openai(msg.model_dump(mode="json", exclude_none=True))
        assert result.tool_calls is not None
        assert len(result.tool_calls) == 1
        tc = result.tool_calls[0]
        assert tc["id"] == "toolu_01A09q90qw90lq917835lq9"
        assert tc["type"] == "function"
        assert tc["function"]["name"] == "get_weather"
        args = json.loads(tc["function"]["arguments"])
        assert args["location"] == "San Francisco"

    def test_multiple_tool_calls(self):
        msg = AnthropicMessage(
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "toolu_001",
                    "name": "search",
                    "input": {"query": "hello"},
                },
                {
                    "id": "toolu_002",
                    "name": "browse",
                    "input": {"url": "https://example.com"},
                },
            ],
        )
        result = _anthropic_message_to_openai(msg.model_dump(mode="json", exclude_none=True))
        assert result.tool_calls is not None
        tcs = result.tool_calls
        assert len(tcs) == 2
        assert tcs[0]["function"]["name"] == "search"
        assert tcs[1]["function"]["name"] == "browse"

    def test_tool_call_with_text_content(self):
        msg = AnthropicMessage(
            role="assistant",
            content="Let me check that for you.",
            tool_calls=[
                {
                    "id": "toolu_001",
                    "name": "search",
                    "input": {"q": "test"},
                }
            ],
        )
        result = _anthropic_message_to_openai(msg.model_dump(mode="json", exclude_none=True))
        assert result.content == "Let me check that for you."
        assert result.tool_calls is not None
        assert len(result.tool_calls) == 1

    def test_assistant_without_tool_calls(self):
        msg = AnthropicMessage(role="assistant", content="Just text.")
        result = _anthropic_message_to_openai(msg.model_dump(mode="json", exclude_none=True))
        assert result.content == "Just text."
        assert result.tool_calls is None

    def test_tool_role_message(self):
        msg = AnthropicMessage(
            role="tool",
            content="Results: 42 degrees",
        )
        result = _anthropic_message_to_openai(msg.model_dump(mode="json", exclude_none=True))
        assert result.role == "tool"
        assert result.content == "Results: 42 degrees"

    def test_content_block_array_with_tool_use(self):
        msg = AnthropicMessage(
            role="assistant",
            content=[
                AnthropicContentBlock(type="text", text="Let me check."),
                AnthropicContentBlock(
                    type="tool_use",
                    id="toolu_001",
                    name="search",
                    input={"q": "test"},
                ),
            ],
        )
        result = _anthropic_message_to_openai(msg.model_dump(mode="json", exclude_none=True))
        assert result.content is not None
        assert isinstance(result.content, list)

    def test_content_block_array_text_only(self):
        msg = AnthropicMessage(
            role="assistant",
            content=[
                AnthropicContentBlock(type="text", text="Hello "),
                AnthropicContentBlock(type="text", text="world"),
            ],
        )
        result = _anthropic_message_to_openai(msg.model_dump(mode="json", exclude_none=True))
        assert result.content == "Hello world"


# ---------------------------------------------------------------------------
# Full AnthropicRequest -> ProxyRequest integration
# ---------------------------------------------------------------------------


class TestAnthropicRequestToProxyRequest:
    """Full integration: AnthropicRequest -> ProxyRequest with tools + tool_choice + tool_calls."""

    def test_full_request_with_tools_and_tool_choice(self):
        request = AnthropicRequest(
            model="claude-sonnet-4-20250514",
            max_tokens=4096,
            system="You are a helpful assistant.",
            messages=[
                AnthropicMessage(role="user", content="What's the weather in SF?"),
                AnthropicMessage(
                    role="assistant",
                    content="",
                    tool_calls=[
                        {
                            "id": "toolu_abc123",
                            "name": "get_weather",
                            "input": {"location": "San Francisco"},
                        }
                    ],
                ),
            ],
            tools=[
                AnthropicTool(
                    name="get_weather",
                    description="Get current weather",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "location": {"type": "string"},
                        },
                        "required": ["location"],
                    },
                )
            ],
            tool_choice=AnthropicToolChoice(type="auto"),
            temperature=0.7,
        )
        result = _anthropic_to_openai(request)

        # Model pass-through
        assert result.model == "claude-sonnet-4-20250514"
        assert result.temperature == 0.7
        assert result.max_tokens == 4096

        # System message prepended
        assert result.messages[0].role == "system"
        assert result.messages[0].content == "You are a helpful assistant."

        # User message
        assert result.messages[1].role == "user"
        assert result.messages[1].content == "What's the weather in SF?"

        # Assistant with tool_calls
        assert result.messages[2].role == "assistant"
        assert result.messages[2].tool_calls is not None
        assert len(result.messages[2].tool_calls) == 1
        assert result.messages[2].tool_calls[0]["function"]["name"] == "get_weather"

        # Tools
        assert result.tools is not None
        assert len(result.tools) == 1
        assert result.tools[0]["function"]["name"] == "get_weather"

        # Tool choice
        assert result.tool_choice == "auto"

    def test_request_without_tools_has_no_tools(self):
        request = AnthropicRequest(
            model="claude-3",
            max_tokens=512,
            messages=[AnthropicMessage(role="user", content="Hello")],
        )
        result = _anthropic_to_openai(request)
        assert result.tools is None
        assert result.tool_choice is None
        assert result.messages[0].role == "user"
        assert result.messages[0].content == "Hello"


# ---------------------------------------------------------------------------
# OpenAI response -> Anthropic format (tool_calls)
# ---------------------------------------------------------------------------


class TestOpenAIResponseToAnthropic:
    """Test that OpenAI responses with tool_calls are translated to Anthropic format."""

    def test_text_only_response(self):
        openai_body = {
            "id": "msg_abc123",
            "object": "chat.completion",
            "model": "gpt-4",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hello world!"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        result = _openai_to_anthropic(openai_body, "claude-3")
        assert result["id"] == "msg_abc123"
        assert result["role"] == "assistant"
        assert result["type"] == "message"
        assert result["stop_reason"] == "end_turn"
        assert len(result["content"]) == 1
        assert result["content"][0]["type"] == "text"
        assert result["content"][0]["text"] == "Hello world!"
        assert result["usage"]["input_tokens"] == 10
        assert result["usage"]["output_tokens"] == 5

    def test_tool_calls_response(self):
        openai_body = {
            "id": "msg_tool123",
            "object": "chat.completion",
            "model": "gpt-4",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_abc123",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": '{"location": "San Francisco"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
        }
        result = _openai_to_anthropic(openai_body, "claude-3")
        assert result["stop_reason"] == "tool_use"
        assert len(result["content"]) == 1
        block = result["content"][0]
        assert block["type"] == "tool_use"
        assert block["id"] == "call_abc123"
        assert block["name"] == "get_weather"
        assert block["input"]["location"] == "San Francisco"

    def test_text_and_tool_calls_response(self):
        openai_body = {
            "id": "msg_both",
            "object": "chat.completion",
            "model": "gpt-4",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "Let me check the weather.",
                        "tool_calls": [
                            {
                                "id": "call_xyz",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": '{"location": "NYC"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 15, "completion_tokens": 8, "total_tokens": 23},
        }
        result = _openai_to_anthropic(openai_body, "claude-3")
        assert result["stop_reason"] == "tool_use"
        assert len(result["content"]) == 2
        assert result["content"][0]["type"] == "text"
        assert result["content"][0]["text"] == "Let me check the weather."
        assert result["content"][1]["type"] == "tool_use"
        assert result["content"][1]["name"] == "get_weather"

    def test_length_finish_reason(self):
        openai_body = {
            "id": "msg_len",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Long response..."},
                    "finish_reason": "length",
                }
            ],
        }
        result = _openai_to_anthropic(openai_body, "claude-3")
        assert result["stop_reason"] == "max_tokens"

    def test_empty_response(self):
        openai_body = {
            "id": "msg_empty",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": None},
                    "finish_reason": "stop",
                }
            ],
        }
        result = _openai_to_anthropic(openai_body, "claude-3")
        assert len(result["content"]) == 1
        assert result["content"][0]["type"] == "text"


# ---------------------------------------------------------------------------
# Streaming tool-call translation
# ---------------------------------------------------------------------------


class TestOpenAIStreamToAnthropic:
    """Test streaming translation with tool calls."""

    def test_stream_with_tool_calls(self):
        chunks = [
            {
                "id": "chatcmpl-1",
                "object": "chat.completion.chunk",
                "created": 123456,
                "model": "gpt-4",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_abc",
                                    "type": "function",
                                    "function": {"name": "get_weather", "arguments": ""},
                                }
                            ],
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "chatcmpl-1",
                "object": "chat.completion.chunk",
                "created": 123456,
                "model": "gpt-4",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": '{"loc'},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "chatcmpl-1",
                "object": "chat.completion.chunk",
                "created": 123456,
                "model": "gpt-4",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": 'ation":"SF"}'},
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            },
            {"usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        ]
        events = _openai_stream_to_anthropic(chunks, "gpt-4")

        # Should have tool_use content_block_start
        tc_starts = [
            e
            for e in events
            if e["type"] == "content_block_start"
            and e.get("content_block", {}).get("type") == "tool_use"
        ]
        assert len(tc_starts) >= 1
        assert tc_starts[0]["content_block"]["name"] == "get_weather"

        # Should have input_json_delta
        json_deltas = [
            e
            for e in events
            if e["type"] == "content_block_delta"
            and e.get("delta", {}).get("type") == "input_json_delta"
        ]
        assert len(json_deltas) >= 1

        # Should have tool_use stop_reason
        msg_deltas = [e for e in events if e["type"] == "message_delta"]
        assert len(msg_deltas) >= 1
        assert msg_deltas[-1]["delta"]["stop_reason"] == "tool_use"

    def test_stream_text_only(self):
        chunks = [
            {
                "id": "chatcmpl-1",
                "object": "chat.completion.chunk",
                "created": 123456,
                "model": "gpt-4",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "Hello"},
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "chatcmpl-1",
                "object": "chat.completion.chunk",
                "created": 123456,
                "model": "gpt-4",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": " world!"},
                        "finish_reason": "stop",
                    }
                ],
            },
            {"usage": {"prompt_tokens": 5, "completion_tokens": 3}},
        ]
        events = _openai_stream_to_anthropic(chunks, "gpt-4")

        # Should have text delta
        text_deltas = [
            e
            for e in events
            if e["type"] == "content_block_delta" and e.get("delta", {}).get("type") == "text_delta"
        ]
        assert len(text_deltas) == 2
        assert text_deltas[0]["delta"]["text"] == "Hello"
        assert text_deltas[1]["delta"]["text"] == " world!"

        # Anthropic protocol: a text content_block_start must open index 0 before
        # any content_block_delta for it, and exactly once per text run.
        text_starts = [
            i
            for i, e in enumerate(events)
            if e["type"] == "content_block_start"
            and e.get("content_block", {}).get("type") == "text"
        ]
        first_text_delta = next(
            i
            for i, e in enumerate(events)
            if e["type"] == "content_block_delta" and e.get("delta", {}).get("type") == "text_delta"
        )
        assert len(text_starts) == 1
        assert text_starts[0] < first_text_delta

        # Should have stop_reason = end_turn
        msg_deltas = [e for e in events if e["type"] == "message_delta"]
        assert msg_deltas[-1]["delta"]["stop_reason"] == "end_turn"

    def test_stream_empty(self):
        events = _openai_stream_to_anthropic([], "gpt-4")
        # Should still produce a minimal message structure
        assert any(e["type"] == "message_start" for e in events)
        assert any(e["type"] == "message_stop" for e in events)


# ---------------------------------------------------------------------------
# AnthropicTool direct conversion
# ---------------------------------------------------------------------------


class TestAnthropicToolToOpenAI:
    """Test individual tool conversion."""

    def test_basic_tool(self):
        tool = {
            "name": "search",
            "description": "Search the web",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
        }
        result = _anthropic_tool_to_openai(tool)
        assert result == {
            "type": "function",
            "function": {
                "name": "search",
                "description": "Search the web",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                },
            },
        }

    def test_tool_without_description(self):
        tool = {"name": "noop", "input_schema": {"type": "object"}}
        result = _anthropic_tool_to_openai(tool)
        assert result["function"]["name"] == "noop"
        assert result["function"]["description"] is None


# ---------------------------------------------------------------------------
# AnthropicMessage model validation
# ---------------------------------------------------------------------------


class TestAnthropicMessageModel:
    """Test that AnthropicMessage model accepts tool-use fields."""

    def test_tool_role_accepted(self):
        msg = AnthropicMessage(role="tool", content="Result data")
        assert msg.role == "tool"

    def test_tool_calls_on_assistant(self):
        msg = AnthropicMessage(
            role="assistant",
            content="",
            tool_calls=[{"id": "t1", "name": "search", "input": {"q": "x"}}],
        )
        assert msg.tool_calls is not None
        assert len(msg.tool_calls) == 1

    def test_content_block_list(self):
        msg = AnthropicMessage(
            role="assistant",
            content=[
                AnthropicContentBlock(type="text", text="Hello"),
            ],
        )
        assert isinstance(msg.content, list)

    def test_tool_result_block(self):
        block = AnthropicContentBlock(
            type="tool_result",
            tool_use_id="toolu_001",
            content="42 degrees",
        )
        assert block.type == "tool_result"
        assert block.tool_use_id == "toolu_001"
        assert block.content == "42 degrees"


# ---------------------------------------------------------------------------
# AnthropicRequest model validation
# ---------------------------------------------------------------------------


class TestAnthropicRequestModel:
    """Test that AnthropicRequest model accepts tools and tool_choice."""

    def test_request_with_tools(self):
        request = AnthropicRequest(
            model="claude-3",
            max_tokens=1024,
            messages=[AnthropicMessage(role="user", content="Test")],
            tools=[AnthropicTool(name="search", input_schema={"type": "object"})],
        )
        assert request.tools is not None
        assert len(request.tools) == 1
        assert request.tools[0].name == "search"

    def test_request_with_tool_choice(self):
        request = AnthropicRequest(
            model="claude-3",
            max_tokens=1024,
            messages=[AnthropicMessage(role="user", content="Test")],
            tool_choice=AnthropicToolChoice(type="tool", name="search"),
        )
        assert request.tool_choice is not None
        assert request.tool_choice.type == "tool"
        assert request.tool_choice.name == "search"

    def test_request_with_system_content_blocks(self):
        request = AnthropicRequest(
            model="claude-3",
            max_tokens=1024,
            messages=[AnthropicMessage(role="user", content="Test")],
            system=[AnthropicContentBlock(type="text", text="You are helpful.")],
        )
        assert isinstance(request.system, list)


# ---------------------------------------------------------------------------
# ProxyRequest tool_choice field
# ---------------------------------------------------------------------------


class TestProxyRequestToolChoice:
    """Test that ProxyRequest accepts tool_choice."""

    def test_proxy_request_with_tool_choice(self):
        pr = ProxyRequest(
            model="gpt-4",
            messages=[ProxyMessage(role="user", content="Test")],
            tool_choice="auto",
        )
        assert pr.tool_choice == "auto"

    def test_proxy_request_with_named_tool_choice(self):
        pr = ProxyRequest(
            model="gpt-4",
            messages=[ProxyMessage(role="user", content="Test")],
            tool_choice={"type": "function", "function": {"name": "search"}},
        )
        assert pr.tool_choice == {"type": "function", "function": {"name": "search"}}

    def test_proxy_request_with_tools(self):
        pr = ProxyRequest(
            model="gpt-4",
            messages=[ProxyMessage(role="user", content="Test")],
            tools=[{"type": "function", "function": {"name": "search", "parameters": {}}}],
        )
        assert pr.tools is not None
        assert len(pr.tools) == 1

    def test_proxy_message_with_tool_calls(self):
        pm = ProxyMessage(
            role="assistant",
            tool_calls=[
                {
                    "id": "call_123",
                    "type": "function",
                    "function": {"name": "search", "arguments": '{"q":"x"}'},
                }
            ],
        )
        assert pm.tool_calls is not None
        assert pm.tool_calls[0]["function"]["name"] == "search"


# ---------------------------------------------------------------------------
# Interleaved streaming state machine (Task 7)
# ---------------------------------------------------------------------------


class TestInterleavedStreamToAnthropic:
    """Test the interleaved streaming state machine for Task 7.

    Verifies that OpenAI responses with interleaved text and tool_calls
    are properly serialized to Anthropic sequential content blocks.
    """

    def test_interleaved_text_then_tool_use(self):
        """Text delta followed by tool_use delta — proper block serialization."""
        chunks = [
            {
                "id": "chatcmpl-1",
                "object": "chat.completion.chunk",
                "created": 123456,
                "model": "gpt-4",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "Let me check."},
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "chatcmpl-1",
                "object": "chat.completion.chunk",
                "created": 123456,
                "model": "gpt-4",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_abc",
                                    "type": "function",
                                    "function": {
                                        "name": "get_weather",
                                        "arguments": "",
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "chatcmpl-1",
                "object": "chat.completion.chunk",
                "created": 123456,
                "model": "gpt-4",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": '{"location": "SF"}'},
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            },
            {"usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        ]
        events = _openai_stream_to_anthropic_interleaved(chunks, "gpt-4")

        # Extract text deltas
        text_deltas = [
            e
            for e in events
            if e["type"] == "content_block_delta" and e.get("delta", {}).get("type") == "text_delta"
        ]
        assert len(text_deltas) == 1
        assert text_deltas[0]["delta"]["text"] == "Let me check."

        # Text block should be stopped before tool_use starts
        text_stops = [e for e in events if e["type"] == "content_block_stop" and e["index"] == 0]
        assert len(text_stops) == 1

        # Tool_use block should have start, delta, stop
        tc_starts = [
            e
            for e in events
            if e["type"] == "content_block_start"
            and e.get("content_block", {}).get("type") == "tool_use"
        ]
        assert len(tc_starts) == 1
        assert tc_starts[0]["content_block"]["name"] == "get_weather"
        assert tc_starts[0]["content_block"]["id"] == "call_abc"
        assert tc_starts[0]["index"] == 1  # index 1 = tool_use block #0

        # Input JSON delta
        json_deltas = [
            e
            for e in events
            if e["type"] == "content_block_delta"
            and e.get("delta", {}).get("type") == "input_json_delta"
        ]
        assert len(json_deltas) == 1
        assert json_deltas[0]["delta"]["partial_json"] == '{"location": "SF"}'

        # Tool_use stop
        tc_stops = [e for e in events if e["type"] == "content_block_stop" and e["index"] == 1]
        assert len(tc_stops) == 1

        # Final message events
        msg_deltas = [e for e in events if e["type"] == "message_delta"]
        assert len(msg_deltas) == 1
        assert msg_deltas[0]["delta"]["stop_reason"] == "tool_use"

        assert any(e["type"] == "message_stop" for e in events)

    def test_interleaved_text_between_tool_calls(self):
        """Text arriving between two tool_use blocks — proper sequencing."""
        chunks = [
            # Tool 0 starts
            {
                "id": "chatcmpl-1",
                "object": "chat.completion.chunk",
                "created": 123456,
                "model": "gpt-4",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_001",
                                    "type": "function",
                                    "function": {
                                        "name": "search",
                                        "arguments": "",
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            # Tool 0 args
            {
                "id": "chatcmpl-1",
                "object": "chat.completion.chunk",
                "created": 123456,
                "model": "gpt-4",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": '{"q":"weather"}'},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            # Text between tools
            {
                "id": "chatcmpl-1",
                "object": "chat.completion.chunk",
                "created": 123456,
                "model": "gpt-4",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "Also checking traffic."},
                        "finish_reason": None,
                    }
                ],
            },
            # Tool 1 starts
            {
                "id": "chatcmpl-1",
                "object": "chat.completion.chunk",
                "created": 123456,
                "model": "gpt-4",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 1,
                                    "id": "call_002",
                                    "type": "function",
                                    "function": {
                                        "name": "get_traffic",
                                        "arguments": "",
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            # Tool 1 args
            {
                "id": "chatcmpl-1",
                "object": "chat.completion.chunk",
                "created": 123456,
                "model": "gpt-4",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 1,
                                    "function": {"arguments": '{"route": "home"}'},
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            },
        ]
        events = _openai_stream_to_anthropic_interleaved(chunks, "gpt-4")

        # Should have 2 tool_use content_block_start events
        tc_starts = [
            e
            for e in events
            if e["type"] == "content_block_start"
            and e.get("content_block", {}).get("type") == "tool_use"
        ]
        assert len(tc_starts) == 2
        assert tc_starts[0]["content_block"]["name"] == "search"
        assert tc_starts[1]["content_block"]["name"] == "get_traffic"

        # Text should be between the two tool_use blocks
        text_deltas = [
            e
            for e in events
            if e["type"] == "content_block_delta" and e.get("delta", {}).get("type") == "text_delta"
        ]
        assert len(text_deltas) == 1
        assert text_deltas[0]["delta"]["text"] == "Also checking traffic."

        # Verify ordering: tool0_start, tool0_delta, tool0_stop, text_delta, text_stop, tool1_start, tool1_delta, tool1_stop
        event_types = [e["type"] for e in events]
        # Text should be between the two tool_use blocks (both start and stop)
        tc0_start_idx = event_types.index("content_block_start")
        tc0_stop_idx = event_types.index("content_block_stop", tc0_start_idx)
        text_idx = event_types.index("content_block_delta", tc0_stop_idx)
        tc1_start_idx = event_types.index("content_block_start", text_idx)
        event_types.index("content_block_stop", tc1_start_idx)
        assert tc0_stop_idx < text_idx < tc1_start_idx

        # Both tool_use blocks should have proper stop events
        assert any(e["type"] == "content_block_stop" and e["index"] == 1 for e in events)
        assert any(e["type"] == "content_block_stop" and e["index"] == 2 for e in events)

    def test_partial_json_chunks(self):
        """Arguments arriving in multiple partial chunks — proper concatenation."""
        chunks = [
            # Tool starts with empty args
            {
                "id": "chatcmpl-1",
                "object": "chat.completion.chunk",
                "created": 123456,
                "model": "gpt-4",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_json",
                                    "type": "function",
                                    "function": {
                                        "name": "compute",
                                        "arguments": "",
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            # Partial arg chunk 1
            {
                "id": "chatcmpl-1",
                "object": "chat.completion.chunk",
                "created": 123456,
                "model": "gpt-4",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": '{"a": '},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            # Partial arg chunk 2
            {
                "id": "chatcmpl-1",
                "object": "chat.completion.chunk",
                "created": 123456,
                "model": "gpt-4",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": "42, "},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            # Partial arg chunk 3
            {
                "id": "chatcmpl-1",
                "object": "chat.completion.chunk",
                "created": 123456,
                "model": "gpt-4",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": '"result"'},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            # Final closing brace
            {
                "id": "chatcmpl-1",
                "object": "chat.completion.chunk",
                "created": 123456,
                "model": "gpt-4",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": '"}'},
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            },
        ]
        events = _openai_stream_to_anthropic_interleaved(chunks, "gpt-4")

        # Should have multiple input_json_delta events
        json_deltas = [
            e
            for e in events
            if e["type"] == "content_block_delta"
            and e.get("delta", {}).get("type") == "input_json_delta"
        ]
        assert len(json_deltas) == 4  # 4 partial chunks

        # Each delta should contain its partial JSON
        assert json_deltas[0]["delta"]["partial_json"] == '{"a": '
        assert json_deltas[1]["delta"]["partial_json"] == "42, "
        assert json_deltas[2]["delta"]["partial_json"] == '"result"'
        assert json_deltas[3]["delta"]["partial_json"] == '"}'

        # Tool block should have proper start/stop
        tc_starts = [
            e
            for e in events
            if e["type"] == "content_block_start"
            and e.get("content_block", {}).get("type") == "tool_use"
        ]
        assert len(tc_starts) == 1
        assert tc_starts[0]["content_block"]["name"] == "compute"
        assert tc_starts[0]["content_block"]["id"] == "call_json"

    def test_tool_calls_only(self):
        """Stream with only tool_calls, no text — proper block serialization."""
        chunks = [
            {
                "id": "chatcmpl-1",
                "object": "chat.completion.chunk",
                "created": 123456,
                "model": "gpt-4",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_001",
                                    "type": "function",
                                    "function": {
                                        "name": "search",
                                        "arguments": '{"q":"test"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            },
        ]
        events = _openai_stream_to_anthropic_interleaved(chunks, "gpt-4")

        assert any(e["type"] == "message_start" for e in events)
        tc_starts = [
            e
            for e in events
            if e["type"] == "content_block_start"
            and e.get("content_block", {}).get("type") == "tool_use"
        ]
        assert len(tc_starts) == 1
        assert tc_starts[0]["content_block"]["name"] == "search"
        assert any(e["type"] == "message_stop" for e in events)

    def test_text_only_stream(self):
        """Stream with only text, no tool_calls — proper block serialization."""
        chunks = [
            {
                "id": "chatcmpl-1",
                "object": "chat.completion.chunk",
                "created": 123456,
                "model": "gpt-4",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "Hello"},
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "chatcmpl-1",
                "object": "chat.completion.chunk",
                "created": 123456,
                "model": "gpt-4",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": " world!"},
                        "finish_reason": "stop",
                    }
                ],
            },
        ]
        events = _openai_stream_to_anthropic_interleaved(chunks, "gpt-4")

        text_deltas = [
            e
            for e in events
            if e["type"] == "content_block_delta" and e.get("delta", {}).get("type") == "text_delta"
        ]
        assert len(text_deltas) == 2
        assert text_deltas[0]["delta"]["text"] == "Hello"
        assert text_deltas[1]["delta"]["text"] == " world!"

        # Anthropic protocol: a text content_block_start must open index 0 before
        # any content_block_delta for it, and exactly once per text run.
        text_starts = [
            i
            for i, e in enumerate(events)
            if e["type"] == "content_block_start"
            and e.get("content_block", {}).get("type") == "text"
        ]
        first_text_delta = next(
            i
            for i, e in enumerate(events)
            if e["type"] == "content_block_delta" and e.get("delta", {}).get("type") == "text_delta"
        )
        assert len(text_starts) == 1
        assert text_starts[0] < first_text_delta

        msg_deltas = [e for e in events if e["type"] == "message_delta"]
        assert msg_deltas[-1]["delta"]["stop_reason"] == "end_turn"

    def test_empty_stream(self):
        """Empty chunk list — minimal message structure."""
        events = _openai_stream_to_anthropic_interleaved([], "gpt-4")
        assert any(e["type"] == "message_start" for e in events)
        assert any(e["type"] == "message_stop" for e in events)

    def test_stop_reason_mapping(self):
        """Verify finish_reason → stop_reason mapping in interleaved stream."""
        # tool_calls → tool_use
        chunks_tc = [
            {
                "id": "chatcmpl-1",
                "object": "chat.completion.chunk",
                "created": 123456,
                "model": "gpt-4",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "noop",
                                        "arguments": "{}",
                                    },
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            }
        ]
        events_tc = _openai_stream_to_anthropic_interleaved(chunks_tc, "gpt-4")
        msg_deltas_tc = [e for e in events_tc if e["type"] == "message_delta"]
        assert msg_deltas_tc[-1]["delta"]["stop_reason"] == "tool_use"

        # length → max_tokens
        chunks_len = [
            {
                "id": "chatcmpl-1",
                "object": "chat.completion.chunk",
                "created": 123456,
                "model": "gpt-4",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "long text..."},
                        "finish_reason": "length",
                    }
                ],
            }
        ]
        events_len = _openai_stream_to_anthropic_interleaved(chunks_len, "gpt-4")
        msg_deltas_len = [e for e in events_len if e["type"] == "message_delta"]
        assert msg_deltas_len[-1]["delta"]["stop_reason"] == "max_tokens"

    def test_content_block_ordering(self):
        """Verify that content blocks are emitted in strict sequential order.

        Anthropic requires sequential content blocks: each block must be
        started (start), have its deltas, then stopped (stop) before the
        next block begins. The state machine emits content_block_stop for
        the current block whenever a new block type arrives.

        Expected order for [text -> tool0 -> text -> tool1]:
          text_delta -> text_stop -> tool0_start -> tool0_delta -> tool0_stop ->
          text_delta -> text_stop -> tool1_start -> tool1_delta -> tool1_stop
        """
        chunks = [
            # Text first
            {
                "id": "chatcmpl-1",
                "object": "chat.completion.chunk",
                "created": 123456,
                "model": "gpt-4",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "Thinking..."},
                        "finish_reason": None,
                    }
                ],
            },
            # Tool 0
            {
                "id": "chatcmpl-1",
                "object": "chat.completion.chunk",
                "created": 123456,
                "model": "gpt-4",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_t0",
                                    "type": "function",
                                    "function": {
                                        "name": "tool_a",
                                        "arguments": '{"x": 1}',
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            # Text after tool
            {
                "id": "chatcmpl-1",
                "object": "chat.completion.chunk",
                "created": 123456,
                "model": "gpt-4",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "Done."},
                        "finish_reason": None,
                    }
                ],
            },
            # Tool 1
            {
                "id": "chatcmpl-1",
                "object": "chat.completion.chunk",
                "created": 123456,
                "model": "gpt-4",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 1,
                                    "id": "call_t1",
                                    "type": "function",
                                    "function": {
                                        "name": "tool_b",
                                        "arguments": '{"y": 2}',
                                    },
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            },
        ]
        events = _openai_stream_to_anthropic_interleaved(chunks, "gpt-4")

        # Get all content_block events in order
        content_events = [
            e
            for e in events
            if e["type"]
            in (
                "content_block_start",
                "content_block_delta",
                "content_block_stop",
            )
        ]

        # Verify sequential order. Each block opens with content_block_start
        # before its delta (Anthropic protocol), so each text run is start ->
        # delta -> stop, same as the tool runs:
        # text(start,delta,stop) -> tool0(start,delta,stop) ->
        # text(start,delta,stop) -> tool1(start,delta,stop)

        # 0-2: first text block at index 0
        assert content_events[0]["type"] == "content_block_start"
        assert content_events[0]["index"] == 0
        assert content_events[0]["content_block"]["type"] == "text"
        assert content_events[1]["type"] == "content_block_delta"
        assert content_events[1]["index"] == 0
        assert content_events[1]["delta"]["type"] == "text_delta"
        assert content_events[2]["type"] == "content_block_stop"
        assert content_events[2]["index"] == 0

        # 3-5: tool0 at index 1
        assert content_events[3]["type"] == "content_block_start"
        assert content_events[3]["index"] == 1
        assert content_events[3]["content_block"]["name"] == "tool_a"
        assert content_events[4]["type"] == "content_block_delta"
        assert content_events[4]["index"] == 1
        assert content_events[4]["delta"]["type"] == "input_json_delta"
        assert content_events[5]["type"] == "content_block_stop"
        assert content_events[5]["index"] == 1

        # 6-8: second text block, reopened at index 0
        assert content_events[6]["type"] == "content_block_start"
        assert content_events[6]["index"] == 0
        assert content_events[6]["content_block"]["type"] == "text"
        assert content_events[7]["type"] == "content_block_delta"
        assert content_events[7]["index"] == 0
        assert content_events[7]["delta"]["type"] == "text_delta"
        assert content_events[8]["type"] == "content_block_stop"
        assert content_events[8]["index"] == 0

        # 9-11: tool1 at index 2
        assert content_events[9]["type"] == "content_block_start"
        assert content_events[9]["index"] == 2
        assert content_events[9]["content_block"]["name"] == "tool_b"
        assert content_events[10]["type"] == "content_block_delta"
        assert content_events[10]["index"] == 2
        assert content_events[10]["delta"]["type"] == "input_json_delta"
        assert content_events[11]["type"] == "content_block_stop"
        assert content_events[11]["index"] == 2
