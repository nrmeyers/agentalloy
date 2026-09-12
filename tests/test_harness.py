"""Harness configuration tests."""

import json
from pathlib import Path

from agentalloy.harness import (
    generate_harness_config,
    write_harness_config,
)


def test_qwen_code_dual_mode() -> None:
    """Qwen Code dual mode generates MCP + proxy config."""
    config = generate_harness_config(
        harness_type="qwen-code",
        mode="dual",
        project_dir="/tmp/test-project",
    )
    assert config.harness_type == "qwen-code"
    assert config.mode == "dual"
    assert len(config.files) > 0
    assert len(config.instructions) > 0

    # Should have MCP settings file
    settings_key = [k for k in config.files if "settings.json" in k]
    assert len(settings_key) == 1
    mcp_data = json.loads(config.files[settings_key[0]])
    assert "mcpServers" in mcp_data
    assert "agentalloy" in mcp_data["mcpServers"]


def test_qwen_code_mcp_only() -> None:
    """Qwen Code MCP-only mode."""
    config = generate_harness_config(
        harness_type="qwen-code",
        mode="mcp",
        project_dir="/tmp/test",
    )
    assert config.mode == "mcp"
    assert len(config.files) > 0
    # Should mention MCP but not proxy instructions
    all_instructions = " ".join(config.instructions)
    assert "MCP" in all_instructions


def test_qwen_code_proxy_only() -> None:
    """Qwen Code proxy-only mode."""
    config = generate_harness_config(
        harness_type="qwen-code",
        mode="proxy",
        project_dir="/tmp/test",
    )
    assert config.mode == "proxy"
    all_instructions = " ".join(config.instructions)
    assert "proxy" in all_instructions.lower()


def test_cursor_generates_rules() -> None:
    """Cursor generates rules file + MCP config."""
    config = generate_harness_config(
        harness_type="cursor",
        mode="dual",
        project_dir="/tmp/test-project",
    )
    assert config.harness_type == "cursor"

    # Should have rules file
    rules_keys = [k for k in config.files if ".mdc" in k]
    assert len(rules_keys) == 1
    rules_content = config.files[rules_keys[0]]
    assert "AgentAlloy" in rules_content
    assert "code_search" in rules_content
    assert "phase_advance" in rules_content

    # Should have MCP config
    mcp_keys = [k for k in config.files if "mcp.json" in k]
    assert len(mcp_keys) == 1


def test_generic_mcp_sse() -> None:
    """Generic MCP generates SSE transport config."""
    config = generate_harness_config(
        harness_type="generic-mcp",
        mode="dual",
        mcp_port=9999,
        project_dir="/tmp/test",
    )
    assert config.harness_type == "generic-mcp"

    mcp_keys = [k for k in config.files if "mcp-config.json" in k]
    assert len(mcp_keys) == 1
    mcp_data = json.loads(config.files[mcp_keys[0]])
    assert mcp_data["mcpServers"]["agentalloy"]["url"] == "http://localhost:9999/sse"
    assert mcp_data["mcpServers"]["agentalloy"]["transport"] == "sse"


def test_write_harness_config(tmp_path: Path) -> None:
    """Write harness config files to disk."""
    config = generate_harness_config(
        harness_type="generic-mcp",
        mode="mcp",
        project_dir=str(tmp_path),
    )
    written = write_harness_config(config)
    assert len(written) > 0

    for path_str in written:
        path = Path(path_str)
        assert path.exists()
        assert path.stat().st_size > 0


def test_write_harness_config_dry_run(tmp_path: Path) -> None:
    """Dry run doesn't write files."""
    config = generate_harness_config(
        harness_type="generic-mcp",
        mode="mcp",
        project_dir=str(tmp_path / "nonexistent"),
    )
    written = write_harness_config(config, dry_run=True)
    assert len(written) > 0
    # All should be prefixed with [dry-run]
    assert all("[dry-run]" in w for w in written)
    # Directory should not exist
    assert not (tmp_path / "nonexistent").exists()


def test_harness_status_no_service() -> None:
    """harness_status handles unreachable service."""
    from agentalloy.harness import harness_status

    status = harness_status(service_port=59999)
    assert status["service"] == "not running"
    assert status["phase"] == "unknown"


def test_all_harness_types_supported() -> None:
    """All harness types generate valid configs."""
    for htype in ["qwen-code", "cursor", "generic-mcp"]:
        config = generate_harness_config(harness_type=htype, project_dir="/tmp/test")
        assert config.harness_type == htype
        assert len(config.instructions) > 0


def test_all_modes_supported() -> None:
    """All integration modes generate valid configs."""
    for mode in ["proxy", "mcp", "dual"]:
        config = generate_harness_config(
            harness_type="qwen-code", mode=mode, project_dir="/tmp/test"
        )
        assert config.mode == mode
