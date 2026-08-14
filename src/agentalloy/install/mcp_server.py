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
import urllib.parse
import urllib.request
from typing import Any

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "agentalloy"
SERVER_VERSION = "0.1.0"

# Compose-target phases exposed to MCP callers: the full Phase vocabulary minus
# "intake" (the session front door — never a compose probe target; the tool
# defaults to "build"). tests/core/test_config_consistency.py asserts this stays in
# lockstep with api.compose_models.Phase.
MCP_PHASES = ("spec", "design", "plan", "build", "qa", "ship", "sdd-fast", "add-skill", "sdd-flow")

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
                "enum": list(MCP_PHASES),
                "description": "Lifecycle phase. Defaults to 'build' if omitted.",
            },
        },
        "required": ["task"],
    },
}

# Query tool actions and their descriptions for the LLM.
_QUERY_ACTIONS = {
    "code_search": "Semantic code search across the indexed codebase.",
    "symbols": "Look up a symbol by fully-qualified name (function, class, etc.).",
    "knowledge_why": "Read the design decision governing a specific symbol.",
    "knowledge_related": "Find decisions related to a topic query.",
    "knowledge_entities": "List typed entity edges (CONSTRAINTS, TOUCHES, REQUIRES, COMMAND, STAKEHOLDER) touching a symbol.",
    "artifact_body": "Read the full body of a recorded phase artifact.",
    "contract_detail": "Read the full detail of a contract by ID.",
    "telemetry": "Recent composition traces with token savings data.",
}

_QUERY_TOOL_DEFINITION: dict[str, Any] = {
    "name": "agentalloy_query",
    "description": (
        "Query the AgentAlloy knowledge and project system. "
        "Use for deep-dive lookups when the state leg summary isn't enough detail."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": list(_QUERY_ACTIONS.keys()),
                "description": "The query to perform. " + " ".join(
                    f"{k}: {v}" for k, v in _QUERY_ACTIONS.items()
                ),
            },
            "query": {
                "type": "string",
                "description": "Search query, symbol FQN, or artifact name (action-specific).",
            },
            "slug": {
                "type": "string",
                "description": "Contract slug (for artifact_body, contract_detail).",
            },
            "phase": {
                "type": "string",
                "description": "Phase scope (for artifact_body when slug is omitted).",
            },
            "k": {
                "type": "integer",
                "description": "Number of results to return (for search actions). Default 10.",
            },
        },
        "required": ["action"],
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
    return _ok(request_id, {"tools": [_TOOL_DEFINITION, _QUERY_TOOL_DEFINITION]})


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
    if name == "get_skill_for":
        return _handle_skill_for(request_id, args, port)
    if name == "agentalloy_query":
        return _handle_query_call(request_id, args, port)
    return _err(request_id, METHOD_NOT_FOUND, f"Unknown tool: {name}")


def _handle_skill_for(request_id: Any, args: dict[str, Any], port: int) -> dict[str, Any]:
    """Handle the get_skill_for tool call."""
    task = args.get("task")
    if not isinstance(task, str) or not task.strip():
        return _err(request_id, INVALID_PARAMS, "'task' must be a non-empty string")
    phase = args.get("phase", "build")
    if phase not in MCP_PHASES:
        return _err(
            request_id,
            INVALID_PARAMS,
            f"'phase' must be one of spec|design|build|qa|ship|sdd-fast; got {phase!r}",
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


def _http_get(port: int, path: str) -> dict[str, Any]:
    """GET a JSON endpoint on the local AgentAlloy service."""
    req = urllib.request.Request(  # noqa: S310 — local-only host
        f"http://127.0.0.1:{port}{path}",
        headers={"Accept": "application/json"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — local
        return json.loads(resp.read())


def _handle_query_call(request_id: Any, args: dict[str, Any], port: int) -> dict[str, Any]:
    """Handle the agentalloy_query tool call — dispatch to action handlers."""
    action = args.get("action")
    if not isinstance(action, str) or action not in _QUERY_ACTIONS:
        return _err(
            request_id,
            INVALID_PARAMS,
            f"'action' must be one of {list(_QUERY_ACTIONS.keys())}",
        )

    query = args.get("query", "")
    slug = args.get("slug", "")
    phase = args.get("phase", "")
    k = args.get("k", 10)

    try:
        if action == "code_search":
            if not query:
                return _err(request_id, INVALID_PARAMS, "'query' required for code_search")
            data = _http_get(port, f"/code/search/semantic?q={_urlencode(query)}&k={k}")
            text = _format_search_results(data)

        elif action == "symbols":
            if not query:
                return _err(request_id, INVALID_PARAMS, "'query' (FQN) required for symbols")
            data = _http_get(port, f"/code/search/symbol?fqn={_urlencode(query)}")
            text = _format_symbol(data)

        elif action == "knowledge_why":
            if not query:
                return _err(request_id, INVALID_PARAMS, "'query' (FQN) required for knowledge_why")
            data = _http_get(
                port,
                f"/code/search/structural?query=governing_decisions&fqn={_urlencode(query)}",
            )
            text = _format_search_results(data.get("results", data))

        elif action == "knowledge_related":
            if not query:
                return _err(request_id, INVALID_PARAMS, "'query' required for knowledge_related")
            data = _http_get(port, f"/code/search/related-decisions?q={_urlencode(query)}&k={k}")
            text = _format_search_results(data)

        elif action == "knowledge_entities":
            if not query:
                return _err(
                    request_id, INVALID_PARAMS, "'query' (FQN or short name) required for knowledge_entities"
                )
            kind_param = args.get("kind")
            kind_qs = f"&kind={_urlencode(kind_param)}" if kind_param else ""
            data = _http_get(port, f"/code/search/entities?query={_urlencode(query)}{kind_qs}")
            text = _format_entity_edges(data)

        elif action == "artifact_body":
            if not phase or not slug or not query:
                return _err(
                    request_id,
                    INVALID_PARAMS,
                    "'phase', 'slug', and 'query' (artifact name) required for artifact_body",
                )
            data = _http_get(port, f"/state/artifact/{phase}/{slug}/{_urlencode(query)}")
            text = data.get("content", json.dumps(data, indent=2))

        elif action == "contract_detail":
            contract_id = slug or query
            if not contract_id:
                return _err(
                    request_id, INVALID_PARAMS, "'slug' or 'query' (contract ID) required"
                )
            data = _http_get(port, f"/contracts/{_urlencode(contract_id)}")
            text = _format_contract(data)

        elif action == "telemetry":
            limit = k if isinstance(k, int) else 10
            phase_filter = f"&phase={phase}" if phase else ""
            data = _http_get(port, f"/telemetry/traces?limit={limit}{phase_filter}")
            text = _format_telemetry(data)

        else:
            return _err(request_id, INVALID_PARAMS, f"Unknown action: {action}")

    except urllib.error.HTTPError as exc:
        return _err(
            request_id,
            INTERNAL_ERROR,
            f"AgentAlloy returned HTTP {exc.code}: {exc.reason}",
        )
    except urllib.error.URLError as exc:
        return _err(
            request_id,
            INTERNAL_ERROR,
            f"AgentAlloy service unreachable on port {port}: {exc.reason}",
        )
    except Exception as exc:  # noqa: BLE001
        return _err(request_id, INTERNAL_ERROR, f"query failed: {exc}")

    return _ok(
        request_id,
        {"content": [{"type": "text", "text": text}], "isError": False},
    )


def _urlencode(s: str) -> str:
    """URL-encode a string for query parameters."""
    return urllib.parse.quote(s, safe="")


def _format_search_results(data: Any) -> str:
    """Format search results as readable text."""
    if not isinstance(data, list):
        return json.dumps(data, indent=2) if data else "No results."
    if not data:
        return "No results."
    lines = []
    for i, r in enumerate(data, 1):
        if isinstance(r, dict):
            heading = r.get("heading") or r.get("qualified_name") or r.get("symbol") or ""
            source = r.get("file_path") or r.get("source") or ""
            snippet = (r.get("snippet") or "")[:200]
            lines.append(f"{i}. {heading}")
            if source:
                lines.append(f"   Source: {source}")
            if snippet:
                lines.append(f"   {snippet}")
            lines.append("")
    return "\n".join(lines)


def _format_symbol(data: Any) -> str:
    """Format a symbol view as readable text."""
    if not isinstance(data, dict):
        return "Symbol not found."
    name = data.get("fqn") or data.get("name") or "unknown"
    kind = data.get("kind") or ""
    source = data.get("file_path") or ""
    lines = [f"Symbol: {name}"]
    if kind:
        lines.append(f"Kind: {kind}")
    if source:
        lines.append(f"File: {source}")
    doc = data.get("docstring") or ""
    if doc:
        lines.append(f"\n{doc[:500]}")
    return "\n".join(lines)


def _format_contract(data: Any) -> str:
    """Format a contract as readable text."""
    if not isinstance(data, dict):
        return "Contract not found."
    lines = [f"Contract: {data.get('contract_id', 'unknown')}"]
    lines.append(f"Phase: {data.get('phase', '?')}")
    lines.append(f"Slug: {data.get('slug', '?')}")
    tags = data.get("domain_tags")
    if tags:
        lines.append(f"Domain tags: {tags}")
    body = data.get("body") or ""
    if body:
        lines.append(f"\n{body[:1000]}")
    return "\n".join(lines)


def _format_entity_edges(data: Any) -> str:
    """Format entity edges as readable text."""
    if not isinstance(data, list):
        return json.dumps(data, indent=2) if data else "No entities found."
    if not data:
        return "No entities found."
    lines = [f"Entity edges ({len(data)}):", ""]
    for e in data[:20]:
        if isinstance(e, dict):
            kind = e.get("kind", "")
            src = e.get("src", "")
            dst = e.get("dst", "")
            span = (e.get("span") or "")[:80]
            lines.append(f"- **{kind}**: `{src}` → `{dst}`")
            if span:
                lines.append(f"  `{span}`")
    return "\n".join(lines)


def _format_telemetry(data: Any) -> str:
    """Format telemetry traces as readable text."""
    if not isinstance(data, dict):
        return json.dumps(data, indent=2)
    traces = data.get("traces", [])
    if not traces:
        return "No recent traces."
    lines = [f"Recent traces ({len(traces)}):"]
    for t in traces[:10]:
        phase = t.get("phase", "?")
        result = t.get("result_type", "?")
        tokens = t.get("tokens_returned", 0)
        saved = t.get("tokens_flat_equivalent", 0)
        lines.append(f"  [{phase}] {result}: {tokens} tokens (flat equiv: {saved})")
    return "\n".join(lines)


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
