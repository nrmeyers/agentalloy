"""Composition injection tests.

Tests the user-message injectors for both proxy surfaces:
``inject_into_anthropic_messages`` (raw payload dict) and
``inject_into_openai_messages`` (typed list[ProxyMessage]), plus the shared
phase-stamped marker helpers.
"""

from __future__ import annotations

from typing import Any

from agentalloy.api.proxy_injection import (
    ANTHROPIC_MARKER_END,
    SYSTEM_MARKER_BEGIN,
    SYSTEM_MARKER_END,
    anthropic_has_marker,
    anthropic_marker_begin,
    inject_into_anthropic_messages,
    inject_into_openai_messages,
)
from agentalloy.api.proxy_models import ProxyMessage

ANTHRO_BLOCK = "# Workflow prose\nDo the design work."


def _text_blocks(content: Any) -> list[dict[str, Any]]:
    """Return only the text blocks from a list-shaped content."""
    assert isinstance(content, list)
    return [b for b in content if isinstance(b, dict) and b.get("type") == "text"]


def _joined_text(content: Any) -> str:
    """Concatenate all text-block text for substring assertions."""
    return "\n".join(b.get("text", "") for b in _text_blocks(content))


class TestAnthropicInjection:
    # ---- TC7: lands in last user message; both content shapes ----

    def test_string_content_injects_into_last_user(self) -> None:
        payload: dict[str, Any] = {
            "model": "claude",
            "system": "SYSTEM PROMPT — do not touch",
            "messages": [
                {"role": "user", "content": "earlier user"},
                {"role": "assistant", "content": "an answer"},
                {"role": "user", "content": "latest user"},
            ],
        }
        result = inject_into_anthropic_messages(payload, ANTHRO_BLOCK, phase="design")

        last = result["messages"][2]
        assert anthropic_marker_begin("design") in last["content"]
        assert ANTHROPIC_MARKER_END in last["content"]
        assert ANTHRO_BLOCK in last["content"]
        assert last["content"].startswith("latest user")
        # Earlier user message untouched.
        assert result["messages"][0]["content"] == "earlier user"
        # System byte-identical.
        assert result["messages"][1]["content"] == "an answer"
        assert result["system"] == "SYSTEM PROMPT — do not touch"
        # Original payload not mutated.
        assert payload["messages"][2]["content"] == "latest user"

    def test_list_content_injects_text_block(self) -> None:
        payload: dict[str, Any] = {
            "model": "claude",
            "system": [{"type": "text", "text": "cached system"}],
            "messages": [
                {"role": "user", "content": "earlier"},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "hi"},
                        {"type": "image", "source": {"data": "x"}},
                    ],
                },
            ],
        }
        result = inject_into_anthropic_messages(payload, ANTHRO_BLOCK, phase="design")

        last = result["messages"][1]
        content = last["content"]
        assert isinstance(content, list)
        # Original blocks preserved (text + image).
        assert content[0] == {"type": "text", "text": "hi"}
        assert content[1] == {"type": "image", "source": {"data": "x"}}
        # New text block appended carrying the marker.
        assert anthropic_marker_begin("design") in _joined_text(content)
        assert ANTHRO_BLOCK in _joined_text(content)
        # System list untouched & earlier message untouched.
        assert result["system"] == [{"type": "text", "text": "cached system"}]
        assert result["messages"][0]["content"] == "earlier"

    def test_no_user_message_returns_unchanged(self) -> None:
        payload: dict[str, Any] = {
            "model": "claude",
            "system": "s",
            "messages": [{"role": "assistant", "content": "a"}],
        }
        result = inject_into_anthropic_messages(payload, ANTHRO_BLOCK, phase="design")
        assert result == payload

    # ---- TC8: idempotent for current phase ----

    def test_idempotent_same_phase_string(self) -> None:
        payload: dict[str, Any] = {
            "model": "claude",
            "system": "s",
            "messages": [{"role": "user", "content": "hi"}],
        }
        once = inject_into_anthropic_messages(payload, ANTHRO_BLOCK, phase="design")
        twice = inject_into_anthropic_messages(once, ANTHRO_BLOCK, phase="design")
        assert twice == once
        assert once["messages"][0]["content"].count(anthropic_marker_begin("design")) == 1

    def test_idempotent_same_phase_list(self) -> None:
        payload: dict[str, Any] = {
            "model": "claude",
            "system": "s",
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        }
        once = inject_into_anthropic_messages(payload, ANTHRO_BLOCK, phase="design")
        twice = inject_into_anthropic_messages(once, ANTHRO_BLOCK, phase="design")
        assert twice == once

    # ---- TC9: stale phase stripped, current phase injected ----

    def test_stale_phase_replaced_string(self) -> None:
        spec_block = f"{anthropic_marker_begin('spec')}\nspec prose\n{ANTHROPIC_MARKER_END}"
        payload: dict[str, Any] = {
            "model": "claude",
            "system": "s",
            "messages": [{"role": "user", "content": f"hi\n\n{spec_block}"}],
        }
        result = inject_into_anthropic_messages(payload, ANTHRO_BLOCK, phase="design")

        content = result["messages"][0]["content"]
        assert anthropic_marker_begin("spec") not in content
        assert "spec prose" not in content
        assert anthropic_marker_begin("design") in content
        assert ANTHRO_BLOCK in content
        # Original user text preserved.
        assert content.startswith("hi")

    def test_stale_phase_replaced_list(self) -> None:
        spec_block = f"{anthropic_marker_begin('spec')}\nspec prose\n{ANTHROPIC_MARKER_END}"
        payload: dict[str, Any] = {
            "model": "claude",
            "system": "s",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "hi"},
                        {"type": "text", "text": spec_block},
                    ],
                }
            ],
        }
        result = inject_into_anthropic_messages(payload, ANTHRO_BLOCK, phase="design")

        content = result["messages"][0]["content"]
        joined = _joined_text(content)
        assert anthropic_marker_begin("spec") not in joined
        assert "spec prose" not in joined
        assert anthropic_marker_begin("design") in joined
        # The plain "hi" block survives; exactly one workflow block remains.
        assert {"type": "text", "text": "hi"} in content
        assert joined.count(anthropic_marker_begin("design")) == 1

    # ---- TC10: system marker, once per session ----

    def test_system_injects_when_absent(self) -> None:
        payload: dict[str, Any] = {
            "model": "claude",
            "system": "cached",
            "messages": [{"role": "user", "content": "hi"}],
        }
        result = inject_into_anthropic_messages(
            payload, "system prose", phase="design", kind="system"
        )
        content = result["messages"][0]["content"]
        assert SYSTEM_MARKER_BEGIN in content
        assert SYSTEM_MARKER_END in content
        assert "system prose" in content
        # Phase markers not used for system kind.
        assert anthropic_marker_begin("design") not in content
        assert result["system"] == "cached"

    def test_system_unchanged_when_marker_present_anywhere(self) -> None:
        sys_block = f"{SYSTEM_MARKER_BEGIN}\nold system\n{SYSTEM_MARKER_END}"
        payload: dict[str, Any] = {
            "model": "claude",
            "system": "cached",
            "messages": [
                {"role": "user", "content": f"first\n\n{sys_block}"},
                {"role": "assistant", "content": "a"},
                {"role": "user", "content": "second"},
            ],
        }
        result = inject_into_anthropic_messages(
            payload, "new system prose", phase="design", kind="system"
        )
        assert result == payload
        assert "new system prose" not in result["messages"][2]["content"]

    # ---- anthropic_has_marker truth table ----

    def test_has_marker_truth_table(self) -> None:
        design_block = f"{anthropic_marker_begin('design')}\nx\n{ANTHROPIC_MARKER_END}"
        sys_block = f"{SYSTEM_MARKER_BEGIN}\ny\n{SYSTEM_MARKER_END}"

        wf_payload: dict[str, Any] = {
            "messages": [{"role": "user", "content": f"hi\n\n{design_block}"}],
        }
        sys_payload: dict[str, Any] = {
            "messages": [{"role": "user", "content": f"hi\n\n{sys_block}"}],
        }
        bare_payload: dict[str, Any] = {
            "messages": [{"role": "user", "content": "hi"}],
        }

        # workflow, phase=None matches ANY workflow phase.
        assert anthropic_has_marker(wf_payload, kind="workflow", phase=None) is True
        # workflow, matching phase.
        assert anthropic_has_marker(wf_payload, kind="workflow", phase="design") is True
        # workflow, non-matching phase.
        assert anthropic_has_marker(wf_payload, kind="workflow", phase="spec") is False
        # system kind does not see a workflow marker.
        assert anthropic_has_marker(wf_payload, kind="system") is False
        # system marker present.
        assert anthropic_has_marker(sys_payload, kind="system") is True
        # workflow phase=None does not see the system marker.
        assert anthropic_has_marker(sys_payload, kind="workflow", phase=None) is False
        # nothing present.
        assert anthropic_has_marker(bare_payload, kind="workflow", phase=None) is False
        assert anthropic_has_marker(bare_payload, kind="system") is False
        # malformed payload (no messages list).
        assert anthropic_has_marker({"messages": "nope"}, kind="workflow") is False


def _openai_text(content: Any) -> str:
    """Concatenate all text-block text from a list-shaped ProxyMessage content."""
    assert isinstance(content, list)
    return "\n".join(b.get("text", "") for b in content if b.get("type") == "text")


class TestOpenAIInjection:
    """OpenAI-surface sibling of TestAnthropicInjection: injects into the last
    user message of a list[ProxyMessage], same phase-stamped markers."""

    # ---- string content: lands in last user message ----

    def test_string_content_injects_into_last_user(self) -> None:
        messages = [
            ProxyMessage(role="system", content="SYSTEM PROMPT — do not touch"),
            ProxyMessage(role="user", content="earlier user"),
            ProxyMessage(role="assistant", content="an answer"),
            ProxyMessage(role="user", content="latest user"),
        ]
        result = inject_into_openai_messages(messages, ANTHRO_BLOCK, phase="design")

        assert result is not None
        last = result[3]
        assert last.role == "user"
        assert isinstance(last.content, str)
        assert anthropic_marker_begin("design") in last.content
        assert ANTHROPIC_MARKER_END in last.content
        assert ANTHRO_BLOCK in last.content
        assert last.content.startswith("latest user")
        # System message + earlier user + assistant untouched.
        assert result[0].content == "SYSTEM PROMPT — do not touch"
        assert result[1].content == "earlier user"
        assert result[2].content == "an answer"
        # Original list not mutated.
        assert messages[3].content == "latest user"

    # ---- list content: appends a text block ----

    def test_list_content_injects_text_block(self) -> None:
        messages = [
            ProxyMessage(role="user", content="earlier"),
            ProxyMessage(
                role="user",
                content=[
                    {"type": "text", "text": "hi"},
                    {"type": "image", "source": {"data": "x"}},
                ],
            ),
        ]
        result = inject_into_openai_messages(messages, ANTHRO_BLOCK, phase="design")

        assert result is not None
        content = result[1].content
        assert isinstance(content, list)
        # Original blocks preserved (text + image).
        assert content[0] == {"type": "text", "text": "hi"}
        assert content[1] == {"type": "image", "source": {"data": "x"}}
        # New text block appended carrying the marker.
        assert anthropic_marker_begin("design") in _openai_text(content)
        assert ANTHRO_BLOCK in _openai_text(content)
        # Earlier message untouched & input not mutated.
        assert result[0].content == "earlier"
        assert messages[1].content == [
            {"type": "text", "text": "hi"},
            {"type": "image", "source": {"data": "x"}},
        ]

    # ---- no user message → None ----

    def test_no_user_message_returns_none(self) -> None:
        messages = [
            ProxyMessage(role="system", content="s"),
            ProxyMessage(role="assistant", content="a"),
        ]
        assert inject_into_openai_messages(messages, ANTHRO_BLOCK, phase="design") is None

    # ---- idempotent for the current phase → None ----

    def test_idempotent_same_phase_string(self) -> None:
        messages = [ProxyMessage(role="user", content="hi")]
        once = inject_into_openai_messages(messages, ANTHRO_BLOCK, phase="design")
        assert once is not None
        twice = inject_into_openai_messages(once, ANTHRO_BLOCK, phase="design")
        assert twice is None
        assert isinstance(once[0].content, str)
        assert once[0].content.count(anthropic_marker_begin("design")) == 1

    def test_idempotent_same_phase_list(self) -> None:
        messages = [
            ProxyMessage(role="user", content=[{"type": "text", "text": "hi"}]),
        ]
        once = inject_into_openai_messages(messages, ANTHRO_BLOCK, phase="design")
        assert once is not None
        twice = inject_into_openai_messages(once, ANTHRO_BLOCK, phase="design")
        assert twice is None

    # ---- stale phase stripped, current phase injected ----

    def test_stale_phase_replaced_string(self) -> None:
        spec_block = f"{anthropic_marker_begin('spec')}\nspec prose\n{ANTHROPIC_MARKER_END}"
        messages = [ProxyMessage(role="user", content=f"hi\n\n{spec_block}")]
        result = inject_into_openai_messages(messages, ANTHRO_BLOCK, phase="design")

        assert result is not None
        content = result[0].content
        assert isinstance(content, str)
        assert anthropic_marker_begin("spec") not in content
        assert "spec prose" not in content
        assert anthropic_marker_begin("design") in content
        assert ANTHRO_BLOCK in content
        assert content.startswith("hi")

    def test_stale_phase_replaced_list(self) -> None:
        spec_block = f"{anthropic_marker_begin('spec')}\nspec prose\n{ANTHROPIC_MARKER_END}"
        messages = [
            ProxyMessage(
                role="user",
                content=[
                    {"type": "text", "text": "hi"},
                    {"type": "text", "text": spec_block},
                ],
            ),
        ]
        result = inject_into_openai_messages(messages, ANTHRO_BLOCK, phase="design")

        assert result is not None
        content = result[0].content
        assert isinstance(content, list)
        joined = _openai_text(content)
        assert anthropic_marker_begin("spec") not in joined
        assert "spec prose" not in joined
        assert anthropic_marker_begin("design") in joined
        # The plain "hi" block survives; exactly one workflow block remains.
        assert {"type": "text", "text": "hi"} in content
        assert joined.count(anthropic_marker_begin("design")) == 1

    # ---- unexpected content shape (None) → None ----

    def test_none_content_returns_none(self) -> None:
        messages = [ProxyMessage(role="user", content=None)]
        assert inject_into_openai_messages(messages, ANTHRO_BLOCK, phase="design") is None
