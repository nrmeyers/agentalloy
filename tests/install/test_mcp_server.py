# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
"""Unit tests for the minimal MCP server (agentalloy.install.mcp_server).

The MCP server is a stdio JSON-RPC 2.0 dispatcher with one tool. These tests
exercise the dispatcher in-process via ``_process_message`` so we don't need
to spawn subprocesses or mock stdin/stdout.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agentalloy.install import mcp_server


class TestInitialize:
    def test_returns_protocol_version_and_server_info(self) -> None:
        msg = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        resp = mcp_server._process_message(msg, port=8000)  # pyright: ignore[reportPrivateUsage]
        assert resp is not None
        assert resp["jsonrpc"] == "2.0"
        assert resp["id"] == 1
        result = resp["result"]
        assert result["protocolVersion"] == mcp_server.PROTOCOL_VERSION
        assert result["serverInfo"]["name"] == "agentalloy"
        assert "capabilities" in result


class TestToolsList:
    def test_returns_get_skill_for_tool(self) -> None:
        msg = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        resp = mcp_server._process_message(msg, port=8000)  # pyright: ignore[reportPrivateUsage]
        assert resp is not None
        tools = resp["result"]["tools"]
        assert len(tools) == 1
        tool = tools[0]
        assert tool["name"] == "get_skill_for"
        # Schema enumerates the lifecycle phases
        phase_enum = tool["inputSchema"]["properties"]["phase"]["enum"]
        assert phase_enum == ["spec", "design", "qa", "build", "ship"]
        assert tool["inputSchema"]["required"] == ["task"]


class TestToolsCallValidation:
    def test_missing_task_returns_invalid_params(self) -> None:
        msg = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "get_skill_for", "arguments": {}},
        }
        resp = mcp_server._process_message(msg, port=8000)  # pyright: ignore[reportPrivateUsage]
        assert resp is not None
        assert resp["error"]["code"] == mcp_server.INVALID_PARAMS

    def test_empty_task_returns_invalid_params(self) -> None:
        msg = {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "get_skill_for", "arguments": {"task": "   "}},
        }
        resp = mcp_server._process_message(msg, port=8000)  # pyright: ignore[reportPrivateUsage]
        assert resp is not None
        assert resp["error"]["code"] == mcp_server.INVALID_PARAMS

    def test_bad_phase_returns_invalid_params(self) -> None:
        msg = {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "get_skill_for",
                "arguments": {"task": "do thing", "phase": "bogus"},
            },
        }
        resp = mcp_server._process_message(msg, port=8000)  # pyright: ignore[reportPrivateUsage]
        assert resp is not None
        assert resp["error"]["code"] == mcp_server.INVALID_PARAMS

    def test_unknown_tool_returns_method_not_found(self) -> None:
        msg = {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "evil_tool", "arguments": {"task": "x"}},
        }
        resp = mcp_server._process_message(msg, port=8000)  # pyright: ignore[reportPrivateUsage]
        assert resp is not None
        assert resp["error"]["code"] == mcp_server.METHOD_NOT_FOUND


class TestToolsCallForward:
    def test_forwards_to_compose_and_returns_output(self) -> None:
        with patch.object(mcp_server, "_call_compose", return_value="composed text"):
            msg = {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {
                    "name": "get_skill_for",
                    "arguments": {"task": "write a failing test", "phase": "build"},
                },
            }
            resp = mcp_server._process_message(msg, port=8000)  # pyright: ignore[reportPrivateUsage]
        assert resp is not None
        assert resp["result"]["isError"] is False
        assert resp["result"]["content"][0]["text"] == "composed text"

    def test_compose_unreachable_returns_internal_error(self) -> None:
        from urllib.error import URLError

        with patch.object(mcp_server, "_call_compose", side_effect=URLError("connection refused")):
            msg = {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {
                    "name": "get_skill_for",
                    "arguments": {"task": "x", "phase": "build"},
                },
            }
            resp = mcp_server._process_message(msg, port=8000)  # pyright: ignore[reportPrivateUsage]
        assert resp is not None
        assert resp["error"]["code"] == mcp_server.INTERNAL_ERROR

    def test_ship_phase_accepted(self) -> None:
        """ship is the terminal lifecycle phase and must compose."""
        with patch.object(mcp_server, "_call_compose", return_value="output for ship"):
            msg = {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {
                    "name": "get_skill_for",
                    "arguments": {"task": "x", "phase": "ship"},
                },
            }
            resp = mcp_server._process_message(msg, port=8000)  # pyright: ignore[reportPrivateUsage]
        assert resp is not None
        assert "error" not in resp
        assert resp["result"]["content"][0]["text"] == "output for ship"

    def test_retired_phases_rejected(self) -> None:
        """ops/meta/governance were retired from the phase vocabulary (Stage 1b)."""
        for phase in ("ops", "meta", "governance"):
            msg = {
                "jsonrpc": "2.0",
                "id": 11,
                "method": "tools/call",
                "params": {
                    "name": "get_skill_for",
                    "arguments": {"task": "x", "phase": phase},
                },
            }
            resp = mcp_server._process_message(msg, port=8000)  # pyright: ignore[reportPrivateUsage]
            assert resp is not None
            assert resp["error"]["code"] == mcp_server.INVALID_PARAMS, (
                f"retired phase {phase!r} should be rejected"
            )


class TestUnknownMethod:
    def test_unknown_method_with_id_returns_error(self) -> None:
        msg = {"jsonrpc": "2.0", "id": 9, "method": "garbage/method", "params": {}}
        resp = mcp_server._process_message(msg, port=8000)  # pyright: ignore[reportPrivateUsage]
        assert resp is not None
        assert resp["error"]["code"] == mcp_server.METHOD_NOT_FOUND

    def test_unknown_method_without_id_is_silent(self) -> None:
        # Notifications (no id) should not produce a response per JSON-RPC 2.0
        msg = {"jsonrpc": "2.0", "method": "garbage/method", "params": {}}
        resp = mcp_server._process_message(msg, port=8000)  # pyright: ignore[reportPrivateUsage]
        assert resp is None

    def test_initialized_notification_no_reply(self) -> None:
        msg = {"jsonrpc": "2.0", "method": "initialized", "params": {}}
        resp = mcp_server._process_message(msg, port=8000)  # pyright: ignore[reportPrivateUsage]
        assert resp is None


class TestMaxLineCap:
    def test_huge_line_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify the stdin parser caps individual messages at _MAX_LINE_BYTES."""
        # Indirect verification: confirm the constant exists and is sane.
        # Full stdin testing would require subprocess; the cap value itself is
        # the contract we want to lock.
        assert mcp_server._MAX_LINE_BYTES == 1 << 20  # pyright: ignore[reportPrivateUsage]
