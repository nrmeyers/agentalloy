"""Harness configuration generator.

Generates harness-specific configuration for integrating AgentAlloy with
coding harnesses (Qwen Code, Cursor, generic MCP clients).

Three integration modes:
- proxy: Harness model routed through AgentAlloy proxy (steering injection)
- mcp: AgentAlloy MCP server provides tools to harness
- dual: Both proxy steering + MCP tools (full integration)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

HarnessType = Literal["qwen-code", "cursor", "generic-mcp"]
IntegrationMode = Literal["proxy", "mcp", "dual"]


@dataclass
class HarnessConfig:
    """Generated harness configuration."""

    harness_type: str
    mode: str
    files: dict[str, str] = field(default_factory=dict)  # path → content
    instructions: list[str] = field(default_factory=list)


def generate_harness_config(
    harness_type: HarnessType,
    mode: IntegrationMode = "dual",
    service_port: int = 48950,
    proxy_port: int = 48953,
    mcp_port: int = 8765,
    project_dir: str | None = None,
) -> HarnessConfig:
    """Generate harness-specific configuration.

    Args:
        harness_type: Target harness (qwen-code, cursor, generic-mcp)
        mode: Integration mode (proxy, mcp, dual)
        service_port: AgentAlloy service port
        proxy_port: AgentAlloy proxy port
        mcp_port: AgentAlloy MCP server port
        project_dir: Project directory for config file paths

    Returns:
        HarnessConfig with file paths, contents, and setup instructions.
    """
    generators = {
        "qwen-code": _generate_qwen_code,
        "cursor": _generate_cursor,
        "generic-mcp": _generate_generic_mcp,
    }

    gen = generators.get(harness_type, _generate_generic_mcp)
    return gen(
        mode=mode,
        service_port=service_port,
        proxy_port=proxy_port,
        mcp_port=mcp_port,
        project_dir=project_dir or ".",
    )


def _generate_qwen_code(
    mode: str,
    service_port: int,
    proxy_port: int,
    mcp_port: int,
    project_dir: str,
) -> HarnessConfig:
    """Generate Qwen Code configuration."""
    files: dict[str, str] = {}
    instructions: list[str] = []

    if mode in ("mcp", "dual"):
        # MCP server configuration for Qwen Code settings.json
        mcp_config = {
            "mcpServers": {
                "agentalloy": {
                    "command": "agentalloy",
                    "args": ["mcp", "--transport", "stdio"],
                    "env": {
                        "AGENTALLOY_SERVICE_PORT": str(service_port),
                    },
                }
            }
        }
        settings_path = str(Path(project_dir) / ".qwen" / "settings.json")
        files[settings_path] = json.dumps(mcp_config, indent=2)
        instructions.append(f"1. MCP config written to {settings_path}")
        instructions.append("2. Restart Qwen Code to pick up the new MCP server")

    if mode in ("proxy", "dual"):
        instructions.append(f"3. Start the steering proxy: agentalloy proxy --port {proxy_port}")
        instructions.append(
            f"4. Configure your model server URL to http://localhost:{proxy_port}/v1"
        )
        instructions.append("   (in Qwen Code settings or environment variable)")

    if mode == "dual":
        instructions.append("5. AgentAlloy provides both MCP tools AND proxy steering")

    return HarnessConfig(
        harness_type="qwen-code",
        mode=mode,
        files=files,
        instructions=instructions,
    )


def _generate_cursor(
    mode: str,
    service_port: int,
    proxy_port: int,
    mcp_port: int,
    project_dir: str,
) -> HarnessConfig:
    """Generate Cursor configuration."""
    files: dict[str, str] = {}
    instructions: list[str] = []

    # Cursor rules file
    rules_content = _build_cursor_rules(service_port, proxy_port)
    rules_path = str(Path(project_dir) / ".cursor" / "rules" / "agentalloy.mdc")
    files[rules_path] = rules_content
    instructions.append(f"1. Cursor rules written to {rules_path}")

    if mode in ("mcp", "dual"):
        # Cursor MCP configuration
        mcp_config = {
            "mcpServers": {
                "agentalloy": {
                    "command": "agentalloy",
                    "args": ["mcp", "--transport", "stdio"],
                }
            }
        }
        mcp_path = str(Path(project_dir) / ".cursor" / "mcp.json")
        files[mcp_path] = json.dumps(mcp_config, indent=2)
        instructions.append(f"2. MCP config written to {mcp_path}")

    if mode in ("proxy", "dual"):
        instructions.append(f"3. Start the steering proxy: agentalloy proxy --port {proxy_port}")
        instructions.append(
            f"4. In Cursor settings, set API Base URL to http://localhost:{proxy_port}/v1"
        )

    instructions.append("5. Restart Cursor to apply configuration changes")

    return HarnessConfig(
        harness_type="cursor",
        mode=mode,
        files=files,
        instructions=instructions,
    )


def _generate_generic_mcp(
    mode: str,
    service_port: int,
    proxy_port: int,
    mcp_port: int,
    project_dir: str,
) -> HarnessConfig:
    """Generate generic MCP client configuration."""
    files: dict[str, str] = {}
    instructions: list[str] = []

    # SSE transport config (for any MCP client)
    mcp_config = {
        "mcpServers": {
            "agentalloy": {
                "url": f"http://localhost:{mcp_port}/sse",
                "transport": "sse",
            }
        }
    }
    config_path = str(Path(project_dir) / ".agentalloy" / "mcp-config.json")
    files[config_path] = json.dumps(mcp_config, indent=2)
    instructions.append(f"1. MCP config written to {config_path}")
    instructions.append(f"2. Start MCP server: agentalloy mcp --transport sse --port {mcp_port}")

    if mode in ("proxy", "dual"):
        instructions.append(f"3. Start steering proxy: agentalloy proxy --port {proxy_port}")
        instructions.append(f"4. Point your model API base to http://localhost:{proxy_port}/v1")

    instructions.append(f"5. AgentAlloy service running at http://localhost:{service_port}")
    instructions.append(f"   Dashboard: http://localhost:{service_port}/dashboard")

    return HarnessConfig(
        harness_type="generic-mcp",
        mode=mode,
        files=files,
        instructions=instructions,
    )


def _build_cursor_rules(service_port: int, proxy_port: int) -> str:
    """Build Cursor rules file content."""
    return f"""---
description: AgentAlloy SDD steering rules
globs:
alwaysApply: true
---

# AgentAlloy Integration

You are working within an AgentAlloy-assisted project. AgentAlloy provides:

- **Phase-aware steering**: The current SDD phase (spec/design/plan/build/qa/ship)
  determines which skills and instructions are active.
- **Contract enforcement**: Build contracts define what you can touch and what to avoid.
- **Tool access**: Use the AgentAlloy MCP tools for code search, state management,
  and phase operations.

## Available MCP Tools

- `code_search` — semantic code search across the indexed codebase
- `symbols` — look up function/class definitions by name
- `get_skill_for` — get phase-appropriate skill instructions
- `contract_add` — record a build contract
- `contract_detail` — read contract details
- `artifact_record` — record a phase artifact
- `phase_advance` — propose a phase transition
- `knowledge_why` — query decision rationale
- `knowledge_related` — find related decisions
- `knowledge_entities` — find entities mentioned in decisions
- `telemetry` — query execution telemetry

## Rules

1. Before starting implementation, check the current phase with `get_skill_for`.
2. Respect build contracts — check `contract_detail` before modifying files.
3. Record artifacts when completing phase work.
4. Never bypass approval gates — if a phase transition is rejected, address the
   missing exit artifact first.
5. Use `code_search` to find relevant code before writing new code.

## Service Endpoints

- Dashboard: http://localhost:{service_port}/dashboard
- Status: http://localhost:{service_port}/status
- Gates: http://localhost:{service_port}/gates
"""


def write_harness_config(config: HarnessConfig, dry_run: bool = False) -> list[str]:
    """Write harness configuration files to disk.

    Returns list of written file paths.
    """
    written: list[str] = []

    for path_str, content in config.files.items():
        path = Path(path_str)
        if dry_run:
            written.append(f"[dry-run] {path}")
            continue

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(str(path))

    return written


def harness_status(service_port: int = 48950) -> dict[str, Any]:
    """Check harness integration status."""
    import urllib.request

    result: dict[str, Any] = {
        "service": "unknown",
        "phase": "unknown",
        "skills": 0,
        "gates": [],
    }

    try:
        req = urllib.request.Request(f"http://localhost:{service_port}/status", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            result["service"] = "running"
            result["phase"] = data.get("phase", "unknown")
            result["skills"] = data.get("skills", 0)
    except Exception:
        result["service"] = "not running"

    try:
        req = urllib.request.Request(f"http://localhost:{service_port}/gates", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            result["gates"] = data.get("gates", [])
    except Exception:
        pass

    return result
