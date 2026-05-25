"""Composition injection tests.

Tests inject_composed_output(), compose_and_inject(), and marker block
handling for system message injection.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from agentalloy.api.compose_models import ComposedResult, EmptyResult
from agentalloy.api.proxy_injection import (
    MARKER_BEGIN,
    MARKER_END,
    compose_and_inject,
    extract_system_message,
    inject_composed_output,
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
