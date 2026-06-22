"""Composition injection tests.

Tests inject_composed_output(), compose_and_inject(), and marker block
handling for system message injection.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from agentalloy.api.compose_models import ComposedResult, EmptyResult
from agentalloy.api.proxy_injection import (
    ANTHROPIC_MARKER_END,
    MARKER_BEGIN,
    MARKER_END,
    SYSTEM_MARKER_BEGIN,
    SYSTEM_MARKER_END,
    anthropic_has_marker,
    anthropic_marker_begin,
    compose_and_inject,
    extract_system_message,
    inject_composed_output,
    inject_into_anthropic_messages,
)
from agentalloy.api.proxy_models import ProxyMessage, ProxyRequest
from agentalloy.api.proxy_signal import SignalResult

OUTPUT = "# Skill content\nSome injected text"
MARKER_BLOCK = f"{MARKER_BEGIN}\n{OUTPUT}\n{MARKER_END}"


def _req(
    messages: list[ProxyMessage] | None = None,
    stream: bool = False,
    metadata: dict[str, Any] | None = None,
) -> ProxyRequest:
    return ProxyRequest(
        model="gpt-4",
        messages=messages or [ProxyMessage(role="user", content="hello")],
        stream=stream,
        temperature=0.7,
        max_tokens=100,
        metadata=metadata,
    )


def _signal(
    compose: bool = True,
    phase: str | None = "build",
    task: str | None = "do stuff",
) -> SignalResult:
    return SignalResult(
        should_compose=compose,
        phase=phase,
        task=task,
    )


class TestExtractSystemMessage:
    def test_returns_first_system(self) -> None:
        msgs = [
            ProxyMessage(role="system", content="sys1"),
            ProxyMessage(role="user", content="u1"),
            ProxyMessage(role="system", content="sys2"),
        ]
        result = extract_system_message(msgs)
        assert result is not None
        assert result.content == "sys1"

    def test_returns_none_when_no_system(self) -> None:
        msgs = [
            ProxyMessage(role="user", content="u1"),
            ProxyMessage(role="assistant", content="a1"),
        ]
        result = extract_system_message(msgs)
        assert result is None


class TestInjectComposedOutput:
    def test_no_system_message_prepends_one(self) -> None:
        req = _req(messages=[ProxyMessage(role="user", content="hello")])
        result = inject_composed_output(req, OUTPUT)

        assert len(result.messages) == 2
        assert result.messages[0].role == "system"
        assert MARKER_BEGIN in result.messages[0].content
        assert MARKER_END in result.messages[0].content
        assert OUTPUT in result.messages[0].content
        assert result.messages[1].role == "user"
        assert result.messages[1].content == "hello"

    def test_existing_system_without_markers_appends(self) -> None:
        req = _req(
            messages=[
                ProxyMessage(role="system", content="You are helpful"),
                ProxyMessage(role="user", content="hello"),
            ]
        )
        result = inject_composed_output(req, OUTPUT)

        assert len(result.messages) == 2
        assert result.messages[0].role == "system"
        assert "You are helpful" in result.messages[0].content
        assert MARKER_BEGIN in result.messages[0].content
        assert MARKER_END in result.messages[0].content

    def test_existing_marker_block_replaced_idempotent(self) -> None:
        old_block = f"{MARKER_BEGIN}\nOld content\n{MARKER_END}"
        req = _req(
            messages=[
                ProxyMessage(role="system", content=f"You are helpful\n\n{old_block}"),
                ProxyMessage(role="user", content="hello"),
            ]
        )
        result = inject_composed_output(req, OUTPUT)

        sys_content = result.messages[0].content
        assert "You are helpful" in sys_content
        assert old_block not in sys_content
        assert MARKER_BLOCK in sys_content
        # Should appear exactly once
        assert sys_content.count(MARKER_BEGIN) == 1

    def test_preserves_optional_fields(self) -> None:
        req = _req(
            stream=True,
            metadata={"cwd": "/tmp/project"},
        )
        result = inject_composed_output(req, OUTPUT)

        assert result.stream is True
        assert result.temperature == 0.7
        assert result.max_tokens == 100
        assert result.metadata == {"cwd": "/tmp/project"}

    def test_returns_new_request_not_mutated(self) -> None:
        req = _req(
            messages=[
                ProxyMessage(role="system", content="original"),
                ProxyMessage(role="user", content="hello"),
            ]
        )
        original_content = req.messages[0].content
        result = inject_composed_output(req, OUTPUT)

        # Original unchanged
        assert req.messages[0].content == original_content
        # New one has markers
        assert MARKER_BEGIN in result.messages[0].content


class TestComposeAndInject:
    def test_no_compose_signal_returns_unchanged(self) -> None:
        req = _req()
        signal = _signal(compose=False)
        orchestrator = MagicMock()

        import asyncio

        result = asyncio.run(compose_and_inject(req, signal, orchestrator))

        assert result.messages[0].content == "hello"
        orchestrator.compose.assert_not_called()

    def test_compose_with_output_injects(self) -> None:
        req = _req()
        signal = _signal()
        orchestrator = MagicMock()
        mock_result = MagicMock(spec=ComposedResult)
        mock_result.output = OUTPUT
        orchestrator.compose = AsyncMock(return_value=mock_result)

        import asyncio

        result = asyncio.run(compose_and_inject(req, signal, orchestrator))

        assert MARKER_BEGIN in result.messages[0].content
        assert OUTPUT in result.messages[0].content

    def test_empty_result_returns_unchanged(self) -> None:
        req = _req()
        signal = _signal()
        orchestrator = MagicMock()
        orchestrator.compose = AsyncMock(
            return_value=EmptyResult(
                task="do stuff",
                phase="build",
                system_fragments=[],
                system_skills_applied=False,
            )
        )

        import asyncio

        result = asyncio.run(compose_and_inject(req, signal, orchestrator))

        # Original request -- no system message added
        assert all(m.role != "system" for m in result.messages)

    def test_compose_exception_returns_unchanged(self) -> None:
        req = _req()
        signal = _signal()
        orchestrator = MagicMock()
        orchestrator.compose = AsyncMock(side_effect=RuntimeError("db error"))

        import asyncio

        result = asyncio.run(compose_and_inject(req, signal, orchestrator))

        # Original request unchanged
        assert len(result.messages) == 1
        assert result.messages[0].role == "user"

    def test_invalid_phase_falls_back_to_build(self) -> None:
        req = _req()
        signal = _signal(phase="unknown_phase")
        orchestrator = MagicMock()
        mock_result = MagicMock(spec=ComposedResult)
        mock_result.output = OUTPUT
        orchestrator.compose = AsyncMock(return_value=mock_result)

        import asyncio

        asyncio.run(compose_and_inject(req, signal, orchestrator))

        # Verify compose was called with phase="build"
        call_args = orchestrator.compose.call_args[0][0]
        assert call_args.phase == "build"

    def test_domain_tags_passed_through(self) -> None:
        req = _req()
        signal = SignalResult(
            should_compose=True,
            phase="build",
            task="do stuff",
            domain_tags=["tag1", "tag2"],
        )
        orchestrator = MagicMock()
        mock_result = MagicMock(spec=ComposedResult)
        mock_result.output = OUTPUT
        orchestrator.compose = AsyncMock(return_value=mock_result)

        import asyncio

        asyncio.run(compose_and_inject(req, signal, orchestrator))

        call_args = orchestrator.compose.call_args[0][0]
        assert call_args.domain_tags == ["tag1", "tag2"]


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
