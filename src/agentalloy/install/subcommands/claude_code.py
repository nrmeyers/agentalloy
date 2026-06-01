"""Claude Code provider — hook wiring and unwiring.

This module provides functions to wire and unwire the AgentAlloy hook
scripts into Claude Code's configuration files.

Key differences from the legacy path:
- The hook script reads JSON from stdin (not a file via CLAUDE_PROMPT_FILE).
- The hook script POSTs to /v1/hook/user-prompt-submit synchronously.
- The hook emits the composed block to stdout for Claude Code to read.
- The settings.json merge removal cleans up hook configuration entries
  that were written by the legacy install path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SENTINEL_BEGIN = "# <!-- BEGIN agentalloy install -->"
SENTINEL_END = "# <!-- END agentalloy install -->"


def _hook_script_path() -> Path:
    """Return the path to the hook script."""
    return Path(__file__).resolve().parent.parent / "agentalloy-hook-claude-code.sh"


def _hooks_config_path() -> Path:
    """Return the path to ~/.claude/claude-code-hooks.json."""
    return Path.home() / ".claude" / "claude-code-hooks.json"


def _settings_json_path() -> Path:
    """Return the path to ~/.claude/settings.json."""
    return Path.home() / ".claude" / "settings.json"


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def _wire_claude_code_hooks(port: int = 47950) -> dict[str, Any]:
    """Wire Claude Code hooks for the AgentAlloy signal layer.

    Writes hook configuration to ~/.claude/claude-code-hooks.json with
    the following hook events:
    - UserPromptSubmit: reads JSON from stdin, POSTs to /v1/hook/user-prompt-submit
    - PreToolUse: reads JSON from stdin, POSTs to /v1/hook/pre-tool-use
    - PostToolUse: reads JSON from stdin, POSTs to /v1/hook/post-tool-use

    The hook script is installed alongside this module and is executable.

    Returns a dict describing what was written (for uninstall tracking).
    """
    hooks_path = _hooks_config_path()
    hooks_path.parent.mkdir(parents=True, exist_ok=True)

    script_path = _hook_script_path()
    script_abs = str(script_path.resolve())

    # Build the hook configuration
    hook_config: dict[str, Any] = {
        "hooks": {
            "UserPromptSubmit": {
                "command": script_abs,
                "env": {
                    "AGENTALLOY_HOOK_URL": f"http://localhost:{port}/v1/hook/user-prompt-submit",
                },
                "description": "AgentAlloy signal-layer hook for user prompts",
            },
            "PreToolUse": {
                "command": script_abs,
                "env": {
                    "AGENTALLOY_HOOK_URL_PRE": f"http://localhost:{port}/v1/hook/pre-tool-use",
                },
                "description": "AgentAlloy signal-layer hook for pre-tool-use",
            },
            "PostToolUse": {
                "command": script_abs,
                "env": {
                    "AGENTALLOY_HOOK_URL_POST": f"http://localhost:{port}/v1/hook/post-tool-use",
                },
                "description": "AgentAlloy signal-layer hook for post-tool-use",
            },
        },
        "schema_version": 1,
    }

    # Check if hooks already exist (idempotency)
    if hooks_path.exists():
        try:
            existing: dict[str, Any] = json.loads(hooks_path.read_text())
            # If the existing config has the same schema, skip
            if existing.get("schema_version") == 1 and "hooks" in existing:
                # Check if the commands match
                existing_commands: set[str] = set()
                for event_cfg in existing.get("hooks", {}).values():
                    cmd = event_cfg.get("command", "")
                    if cmd:
                        existing_commands.add(cmd)
                if script_abs in existing_commands:
                    # Already wired — just update port if needed
                    for _event_name, event_cfg in existing["hooks"].items():
                        env: dict[str, Any] = event_cfg.get("env", {})
                        if "AGENTALLOY_HOOK_URL" in env:
                            env["AGENTALLOY_HOOK_URL"] = (
                                f"http://localhost:{port}/v1/hook/user-prompt-submit"
                            )
                        if "AGENTALLOY_HOOK_URL_PRE" in env:
                            env["AGENTALLOY_HOOK_URL_PRE"] = (
                                f"http://localhost:{port}/v1/hook/pre-tool-use"
                            )
                        if "AGENTALLOY_HOOK_URL_POST" in env:
                            env["AGENTALLOY_HOOK_URL_POST"] = (
                                f"http://localhost:{port}/v1/hook/post-tool-use"
                            )
                        event_cfg["env"] = env
                    hooks_path.write_text(json.dumps(existing, indent=2) + "\n")
                    return {
                        "path": str(hooks_path),
                        "action": "idempotent_skip",
                        "script": script_abs,
                    }
        except (json.JSONDecodeError, OSError):
            pass

    # Write the hook configuration
    hooks_path.write_text(json.dumps(hook_config, indent=2) + "\n")

    return {
        "path": str(hooks_path),
        "action": "wrote_hooks_config",
        "script": script_abs,
        "hook_events": list(hook_config["hooks"].keys()),
    }


def _unwire_claude_code_hooks() -> list[dict[str, Any]]:
    """Unwire Claude Code hooks — remove the hooks config file.

    Returns a list of dicts describing what was removed.
    """
    hooks_path = _hooks_config_path()
    removed: list[dict[str, Any]] = []

    if hooks_path.exists():
        hooks_path.unlink()
        removed.append(
            {
                "path": str(hooks_path),
                "action": "removed_hooks_config",
            }
        )

    # Also clean up settings.json merge entries (legacy path)
    removed.extend(_unwire_claude_code_settings_json())

    return removed


def _unwire_claude_code_settings_json() -> list[dict[str, Any]]:
    """Remove AgentAlloy hook entries from ~/.claude/settings.json.

    The legacy install path may have written hook-related entries into
    settings.json. This function removes them using sentinel markers
    for safe, bounded cleanup.

    Returns a list of dicts describing what was removed.
    """
    settings_path = _settings_json_path()
    removed: list[dict[str, Any]] = []

    if not settings_path.exists():
        return removed

    try:
        content = settings_path.read_text()
        data = json.loads(content)
    except (json.JSONDecodeError, OSError):
        return removed

    # Sentinel-bounded removal for settings.json
    # We use a marker key to track what we installed
    sentinel_key = "_agentalloy_install_marker"
    if sentinel_key in data:
        marker = data.pop(sentinel_key, {})
        removed.append(
            {
                "path": str(settings_path),
                "action": "removed_marker",
                "marker": marker,
            }
        )

    # Remove hook-related entries that may have been written by the legacy path
    keys_to_remove: list[str] = []
    for key in data:
        if key.startswith("hooks.") or key == "hooks":
            keys_to_remove.append(key)

    for key in keys_to_remove:
        del data[key]  # pyright: ignore[reportUnknownMemberType]
        removed.append(
            {
                "path": str(settings_path),
                "action": "removed_key",
                "key": key,
            }
        )

    # Also remove any "hooks" top-level key
    if "hooks" in data:
        del data["hooks"]
        removed.append(
            {
                "path": str(settings_path),
                "action": "removed_key",
                "key": "hooks",
            }
        )

    if removed:
        # Write back the cleaned settings.json
        settings_path.write_text(json.dumps(data, indent=2) + "\n")

    return removed


# ---------------------------------------------------------------------------
# Legacy settings.json merge removal
# ---------------------------------------------------------------------------


def _remove_hooks_from_settings_json(settings_path: Path) -> list[dict[str, Any]]:
    """Remove AgentAlloy hook configuration from settings.json.

    The legacy install path wrote hook entries into ~/.claude/settings.json.
    This function removes them using sentinel-bounded cleanup.

    Returns a list of dicts describing what was removed.
    """
    removed: list[dict[str, Any]] = []

    if not settings_path.exists():
        return removed

    try:
        content = settings_path.read_text()
        data = json.loads(content)
    except (json.JSONDecodeError, OSError):
        return removed

    # Check for sentinel-bounded block in the content
    sentinel_begin = SENTINEL_BEGIN
    sentinel_end = SENTINEL_END

    if sentinel_begin in content and sentinel_end in content:
        # Remove the sentinel-bounded block from the JSON string
        begin_idx = content.index(sentinel_begin)
        end_idx = content.index(sentinel_end) + len(sentinel_end)
        new_content = content[:begin_idx] + content[end_idx:]

        # Re-parse and write back
        try:
            new_data = json.loads(new_content)
            settings_path.write_text(json.dumps(new_data, indent=2) + "\n")
            removed.append(
                {
                    "path": str(settings_path),
                    "action": "removed_sentinel_block",
                }
            )
        except json.JSONDecodeError:
            # If re-parsing fails, fall back to key-based removal
            pass

    # Key-based removal (fallback)
    keys_to_remove: list[str] = []
    for key in data:
        if key.startswith("hooks") or key == "claude_code_hooks":
            keys_to_remove.append(key)

    for key in keys_to_remove:
        del data[key]  # pyright: ignore[reportUnknownMemberType]
        removed.append(
            {
                "path": str(settings_path),
                "action": "removed_key",
                "key": key,
            }
        )

    if keys_to_remove:
        settings_path.write_text(json.dumps(data, indent=2) + "\n")

    return removed


def remove_hooks_from_settings_json() -> list[dict[str, Any]]:
    """Convenience wrapper to remove hooks from the default settings.json."""
    return _remove_hooks_from_settings_json(_settings_json_path())


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def wire_claude_code_hooks(port: int = 47950) -> dict[str, Any]:
    """Public entry point for wiring Claude Code hooks.

    This is the function that gets called from the legacy path in
    wire_harness.py when --legacy is specified for the claude-code harness.
    """
    result = _wire_claude_code_hooks(port)
    return result


def unwire_claude_code_hooks() -> list[dict[str, Any]]:
    """Public entry point for unwiring Claude Code hooks."""
    return _unwire_claude_code_hooks()
