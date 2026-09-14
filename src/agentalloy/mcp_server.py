"""MCP server: exposes AgentAlloy tools via Model Context Protocol.

Tool surface is deliberately small by default. Every registered schema is
resent to the main model on every turn, so the default ("slim") exposes
only code_search + contract_detail (~230 tokens). The full 13-tool surface
(~1.5k tokens/turn) is opt-in via AGENTALLOY_MCP_TOOLS=full — state work
(contracts, phase, skills) belongs to the sidecar orchestrator, out of the
main model's line of sight.

Thin stdio↔HTTP bridge: every tool call is delegated to the v2 service's
POST /tool endpoint. This process never opens state.duck or builds the
index — the service owns the single RW DuckDB handle and the shared code
graph index, and while it holds that handle no other process can open the
file at all (empirically confirmed). The bridge therefore works even
while the service is running, where a direct store open would crash.

Usage:
    # stdio transport (for harness integration)
    agentalloy mcp --transport stdio

    # SSE transport (for network access)
    agentalloy mcp --transport sse --port 8765
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer

logging.getLogger("httpx").setLevel(logging.WARNING)

# Create the MCP server
mcp_server = MCPServer(
    name="agentalloy",
    title="AgentAlloy",
    description="SDD phase management + code intelligence tools",
    version="0.3.0",
)

_SERVICE_URL = f"http://127.0.0.1:{os.environ.get('AGENTALLOY_SERVICE_PORT', '48950')}"
_TIMEOUT = 120.0

# Tool surface. slim = the two tools whose value the steering brief can
# only point at, not carry (on-demand code lookup + contract bodies).
_SLIM_TOOLS = frozenset({"code_search", "contract_detail"})
_TOOL_MODE = os.environ.get("AGENTALLOY_MCP_TOOLS", "slim").strip().lower()


def _tool(name: str, description: str) -> Any:
    """Register a tool only when the active surface includes it."""
    if _TOOL_MODE != "full" and name not in _SLIM_TOOLS:
        return lambda fn: fn
    return mcp_server.tool(name=name, description=description)


def _project() -> str:
    """Project scope for state tools. `agentalloy wire` sets AGENTALLOY_PROJECT in
    this server's env; fall back to deriving it from the cwd (the harness
    spawns us in the wired repo). Same derivation as registry.project_key."""
    env = os.environ.get("AGENTALLOY_PROJECT", "")
    if env:
        return env
    try:
        from agentalloy.registry import project_key

        return project_key(os.getcwd())
    except OSError:
        return ""


def _call(name: str, args: dict[str, Any]) -> str:
    """Delegate a single tool execution to the v2 service."""
    try:
        response = httpx.post(
            f"{_SERVICE_URL}/tool",
            json={"name": name, "args": json.dumps(args), "project": _project()},
            timeout=_TIMEOUT,
        )
        response.raise_for_status()
    except httpx.HTTPError as e:
        return f"error: agentalloy service unreachable at {_SERVICE_URL}: {e}"
    payload = response.json()
    if not payload.get("ok"):
        return f"error: {payload.get('error', 'unknown tool error')}"
    return str(payload.get("result", ""))


# --- READ tools ---


@_tool(
    name="code_search",
    description="Semantic code search. Returns relevant code snippets from the repository.",
)
def mcp_code_search(query: str, k: int = 10) -> str:
    """Search code in the repository."""
    return _call("code_search", {"query": query, "k": k})


@_tool(
    name="symbols",
    description="Look up symbols (functions, classes, methods) by fully-qualified name.",
)
def mcp_symbols(fqn: str = "") -> str:
    """Look up symbols by FQN."""
    return _call("symbols", {"fqn": fqn})


@_tool(
    name="knowledge_why",
    description="Retrieve architectural decision records for a symbol or topic.",
)
def mcp_knowledge_why(fqn: str = "") -> str:
    """Get decision records."""
    return _call("knowledge_why", {"fqn": fqn})


@_tool(
    name="knowledge_related",
    description="Find decisions related to a query or symbol.",
)
def mcp_knowledge_related(query: str = "") -> str:
    """Find related decisions."""
    return _call("knowledge_related", {"query": query})


@_tool(
    name="knowledge_entities",
    description="List entities (classes, modules, interfaces) related to a symbol.",
)
def mcp_knowledge_entities(fqn: str = "") -> str:
    """List related entities."""
    return _call("knowledge_entities", {"fqn": fqn})


@_tool(
    name="artifact_body",
    description="Retrieve a recorded artifact by phase and name.",
)
def mcp_artifact_body(phase: str, name: str) -> str:
    """Get artifact body."""
    return _call("artifact_body", {"phase": phase, "name": name})


@_tool(
    name="contract_detail",
    description="Retrieve a contract by slug — shows domain tags, touches, and constraints.",
)
def mcp_contract_detail(slug: str) -> str:
    """Get contract details."""
    return _call("contract_detail", {"slug": slug})


@_tool(
    name="telemetry",
    description="Retrieve recent telemetry traces for debugging.",
)
def mcp_telemetry(k: int = 10, phase: str | None = None) -> str:
    """Get telemetry traces."""
    args: dict[str, Any] = {"k": k}
    if phase:
        args["phase"] = phase
    return _call("telemetry", args)


@_tool(
    name="get_skill_for",
    description="Find relevant skills for a task in the current phase.",
)
def mcp_get_skill_for(task: str = "", phase: str = "") -> str:
    """Get skill candidates."""
    return _call("get_skill_for", {"task": task, "phase": phase})


# --- STATE tools ---


@_tool(
    name="contract_add",
    description="Add a new contract with domain tags and touch points.",
)
def mcp_contract_add(slug: str, domain_tags: list[str], touches: str) -> str:
    """Add a contract."""
    return _call(
        "contract_add",
        {"slug": slug, "domain_tags": domain_tags, "touches": touches},
    )


@_tool(
    name="artifact_record",
    description="Record an artifact (build log, design doc, test result) for a phase.",
)
def mcp_artifact_record(phase: str, name: str, body: str) -> str:
    """Record an artifact."""
    return _call("artifact_record", {"phase": phase, "name": name, "body": body})


@_tool(
    name="phase_advance",
    description="Advance to the next SDD phase. Requires exit artifact for current phase.",
)
def mcp_phase_advance(target: str, approved: bool = False) -> str:
    """Advance phase."""
    return _call("phase_advance", {"target": target, "approved": approved})


@_tool(
    name="phase_reset",
    description="Reset the lifecycle to intake and clear approvals (operator-only). "
    "Contracts and artifacts are kept.",
)
def mcp_phase_reset() -> str:
    """Reset phase to intake."""
    return _call("phase_reset", {})


def run(transport: str = "stdio", port: int = 8765) -> None:
    """Start the bridge on the given transport."""
    if transport == "stdio":
        asyncio.run(mcp_server.run_stdio_async())
    else:
        asyncio.run(mcp_server.run_sse_async(port=port))


def main() -> None:
    """Run the MCP server (stdio or SSE) as a bridge to the v2 service."""
    parser = argparse.ArgumentParser(description="AgentAlloy MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="Transport protocol (default: stdio)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Port for SSE transport (default: 8765)",
    )
    args = parser.parse_args()
    run(args.transport, args.port)


if __name__ == "__main__":
    main()
