# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnknownLambdaType=false, reportArgumentType=false
"""Minimal MCP server for the AgentAlloy install MCP fallback.

Implements just enough of the Model Context Protocol (stdio JSON-RPC 2.0)
to expose one tool — ``get_skill_for(task, phase)`` — that forwards to
the local ``/compose`` endpoint and returns the composed fragments.

Used by harnesses that opt into the strict-tools fallback variant of
``wire-harness`` (per `docs/install/harness-catalog.md` § "MCP fallback").

Protocol reference: https://spec.modelcontextprotocol.io/

Run via::

    python -m agentalloy.install.mcp_server --port 47950

The server reads JSON-RPC requests from stdin (one per line) and writes
responses to stdout. Errors and progress are logged to stderr.

This module is dependency-free — no MCP SDK required — so it inherits
no network/runtime cost beyond the AgentAlloy package itself.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "agentalloy"
SERVER_VERSION = "0.1.0"

# JSON-RPC error codes (a subset of the standard set)
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


_TOOL_DEFINITION: dict[str, Any] = {
    "name": "get_skill_for",
    "description": (
        "Fetch composed skill fragments for a given coding task and phase. "
        "Returns concatenated raw fragments from the local AgentAlloy corpus."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "One-sentence description of the coding task.",
            },
            "phase": {
                "type": "string",
                "enum": ["spec", "design", "build", "qa", "ship"],
                "description": "Lifecycle phase. Defaults to 'build' if omitted.",
            },
        },
        "required": ["task"],
    },
}


def _ok(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _err(request_id: Any, code: int, message: str, data: Any | None = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": err}


def _handle_initialize(request_id: Any, _params: dict[str, Any]) -> dict[str, Any]:
    return _ok(
        request_id,
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        },
    )


def _handle_tools_list(request_id: Any, _params: dict[str, Any]) -> dict[str, Any]:
    return _ok(request_id, {"tools": [_TOOL_DEFINITION]})


def _call_compose(port: int, task: str, phase: str) -> str:
    """POST to the local AgentAlloy /compose endpoint, return the ``output`` field."""
    body = json.dumps({"task": task, "phase": phase}).encode()
    req = urllib.request.Request(  # noqa: S310 — local-only host
        f"http://127.0.0.1:{port}/compose",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — local
        payload = json.loads(resp.read())
    return str(payload.get("output", ""))


def _handle_tools_call(request_id: Any, params: dict[str, Any], port: int) -> dict[str, Any]:
    name = params.get("name")
    args = params.get("arguments") or {}
    if name != "get_skill_for":
        return _err(request_id, METHOD_NOT_FOUND, f"Unknown tool: {name}")
    task = args.get("task")
    if not isinstance(task, str) or not task.strip():
        return _err(request_id, INVALID_PARAMS, "'task' must be a non-empty string")
    phase = args.get("phase", "build")
    if phase not in ("spec", "design", "build", "qa", "ship"):
        return _err(
            request_id,
            INVALID_PARAMS,
            f"'phase' must be one of spec|design|build|qa|ship; got {phase!r}",
        )
    try:
        text = _call_compose(port, task, phase)
    except urllib.error.URLError as exc:
        return _err(
            request_id,
            INTERNAL_ERROR,
            f"AgentAlloy /compose unreachable on port {port}: {exc.reason}",
        )
    except Exception as exc:  # noqa: BLE001 — surface any unexpected failure
        return _err(request_id, INTERNAL_ERROR, f"compose call failed: {exc}")
    return _ok(
        request_id,
        {
            "content": [{"type": "text", "text": text}],
            "isError": False,
        },
    )


_HANDLERS: dict[str, Any] = {
    "initialize": lambda rid, p, _port: _handle_initialize(rid, p),
    "initialized": lambda _rid, _p, _port: None,  # notification — no reply
    "ping": lambda rid, _p, _port: _ok(rid, {}),
    "tools/list": lambda rid, p, _port: _handle_tools_list(rid, p),
    "tools/call": lambda rid, p, port: _handle_tools_call(rid, p, port),
}


def _process_message(msg: dict[str, Any], port: int) -> dict[str, Any] | None:
    """Dispatch a single JSON-RPC message. Returns response or None for notifications."""
    method = msg.get("method")
    rid = msg.get("id")
    raw_params = msg.get("params")
    # JSON-RPC permits omitted/null/object/array params. The handlers
    # below all `params.get(...)`; coerce non-dict shapes to {} so a
    # hostile client sending `"params": [1,2,3]` or `"params": 42`
    # can't trigger an AttributeError that escapes _process_message
    # and kills the serve() loop.
    params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}

    handler = _HANDLERS.get(method)
    if handler is None:
        if rid is None:
            return None  # unknown notification — ignore
        return _err(rid, METHOD_NOT_FOUND, f"Unknown method: {method}")

    try:
        return handler(rid, params, port)
    except Exception as exc:  # noqa: BLE001 — keep the dispatcher loop alive
        if rid is None:
            return None
        return _err(rid, INTERNAL_ERROR, f"Handler crashed: {exc}")


# Cap on a single JSON-RPC message size. MCP messages are typically <10 KB
# (one tool call). 1 MB is generous; bigger inputs are almost certainly hostile
# or buggy and would otherwise let a single huge line consume RAM until EOF.
_MAX_LINE_BYTES = 1 << 20  # 1 MiB


def serve(port: int) -> int:
    """Read JSON-RPC messages from stdin, write responses to stdout.

    Newline-delimited JSON per the MCP 2024-11-05 stdio transport.
    Each line is hard-capped at ``_MAX_LINE_BYTES`` (1 MiB); oversized lines
    return a PARSE_ERROR and are otherwise discarded so the server doesn't
    block on adversarial unbounded input.
    """
    print(
        f"agentalloy MCP server: forwarding to /compose on port {port}",
        file=sys.stderr,
        flush=True,
    )
    # Try to set utf-8 on stdin so non-ASCII task strings don't break decoding.
    import contextlib

    with contextlib.suppress(Exception):
        sys.stdin.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

    while True:
        line = sys.stdin.readline(_MAX_LINE_BYTES + 1)
        if not line:
            break  # EOF
        # If we hit the cap mid-line, drain the rest of that line and reject.
        if len(line) > _MAX_LINE_BYTES:
            # Drain until newline so we resync on the next message.
            while line and not line.endswith("\n"):
                line = sys.stdin.readline(_MAX_LINE_BYTES + 1)
            response = _err(None, PARSE_ERROR, "JSON-RPC message exceeds 1 MiB cap")
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
            continue

        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            response = _err(None, PARSE_ERROR, f"JSON parse error: {exc}")
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
            continue

        response = _process_message(msg, port)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agentalloy.install.mcp_server",
        description="Minimal MCP server forwarding to local AgentAlloy /compose.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=47950,
        help="Local AgentAlloy service port (default: 47950).",
    )
    args = parser.parse_args(argv)
    return serve(args.port)


if __name__ == "__main__":
    raise SystemExit(main())
