"""Upstream payload construction: no explicit nulls on optional message fields.

Strict upstreams (llama.cpp) reject ``"tool_call_id": null`` and friends with
"type must be string, but is null" — a 500 on every proxied request.
"""

from __future__ import annotations

from agentalloy.api.proxy_models import ProxyRequest
from agentalloy.api.proxy_router import _build_payload


def _payload(messages: list[dict], **kwargs) -> dict:
    request = ProxyRequest(model="agentalloy-proxy", messages=messages, **kwargs)
    return _build_payload(request, upstream_model="qwen")


class TestBuildPayloadExcludesNulls:
    def test_plain_user_message_has_no_null_fields(self) -> None:
        payload = _payload([{"role": "user", "content": "hi"}])
        (msg,) = payload["messages"]
        assert msg == {"role": "user", "content": "hi"}

    def test_system_and_user_messages_carry_only_set_fields(self) -> None:
        payload = _payload(
            [
                {"role": "system", "content": "be terse"},
                {"role": "user", "content": "hi"},
            ]
        )
        for msg in payload["messages"]:
            assert None not in msg.values()
            assert set(msg) == {"role", "content"}

    def test_tool_flow_fields_are_preserved_when_set(self) -> None:
        tool_calls = [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "f", "arguments": "{}"},
            }
        ]
        payload = _payload(
            [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "calling", "tool_calls": tool_calls},
                {"role": "tool", "content": "result", "tool_call_id": "call_1"},
            ]
        )
        user, assistant, tool = payload["messages"]
        assert "tool_call_id" not in user
        assert assistant["tool_calls"] == tool_calls
        assert "tool_call_id" not in assistant
        assert tool["tool_call_id"] == "call_1"
        for msg in payload["messages"]:
            assert None not in msg.values()

    def test_assistant_tool_call_with_no_content_omits_content_key(self) -> None:
        payload = _payload(
            [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "f", "arguments": "{}"},
                        }
                    ],
                }
            ]
        )
        (msg,) = payload["messages"]
        assert "content" not in msg
