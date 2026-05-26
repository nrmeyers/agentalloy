"""Uninstall logic for proxy configs.

Functions to reverse each proxy wiring operation. Each uses the same sentinel comments
as the corresponding wire function for bounded removal.
"""

from __future__ import annotations

import json
from pathlib import Path


def _remove_sentinel_block(content: str) -> str:
    """Remove content between agentalloy sentinels.

    Uses the same sentinels as wire_harness.py for all blocks.
    """
    begin = "<!-- BEGIN agentalloy install -->"
    end = "<!-- END agentalloy install -->"

    if begin not in content or end not in content:
        return content
    b = content.index(begin)
    e = content.index(end) + len(end)
    # Consume trailing newline
    if e < len(content) and content[e] == "\n":
        e += 1
    # Consume blank line before block if present
    if b > 0 and content[b - 1] == "\n":
        b -= 1
        if b > 0 and content[b - 1] == "\n":
            b -= 1
    result = content[:b] + content[e:]
    # Clean up double blank lines
    while "\n\n\n" in result:
        result = result.replace("\n\n\n", "\n\n")
    return result


def _unwire_proxy_aider(root: Path) -> list[Path]:
    """Remove aider proxy config from .aider.conf.yml."""
    conf_path = root / ".aider.conf.yml"
    if not conf_path.exists():
        return []
    content = conf_path.read_text()
    # Remove between sentinel comments
    new_content = _remove_sentinel_block(content)
    conf_path.write_text(new_content)
    # Also remove instructions file
    instr_path = root / ".agentalloy-aider-instructions.md"
    if instr_path.exists():
        instr_path.unlink()
        return [conf_path, instr_path]
    return [conf_path]


def _unwire_proxy_hermes_agent(scope: str, root: Path) -> list[Path]:
    """Remove hermes-agent proxy config from config.yaml."""
    config_path = Path.home() / ".hermes" / "config.yaml" if scope == "user" else root / "AGENTS.md"
    if not config_path.exists():
        return []
    content = config_path.read_text()
    new_content = _remove_sentinel_block(content)
    config_path.write_text(new_content)
    return [config_path]


def _unwire_proxy_opencode(root: Path) -> list[Path]:
    """Remove opencode proxy env file."""
    env_path = root / ".opencode" / ".agentalloy-env"
    prompt_path = root / ".opencode" / "system-prompt.md"
    removed: list[Path] = []  # type: ignore[reportUnknownVariableType]
    if env_path.exists():
        env_path.unlink()
        removed.append(env_path)
    if prompt_path.exists():
        prompt_path.unlink()
        removed.append(prompt_path)
    return removed


def _unwire_proxy_claude_code(root: Path) -> list[Path]:
    """Remove claude-code env file and shell profile entries."""
    env_path = Path.home() / ".agentalloy" / "claude-code-env.sh"
    if env_path.exists():
        env_path.unlink()
        # Print instructions for shell profile cleanup
        print("Remove the source line from .bashrc/.zshrc manually:")
        print("  # AgentAlloy: claude-code proxy env")
        return [env_path]
    return []


def _unwire_proxy_cline(root: Path) -> list[Path]:
    """Remove cline settings file."""
    settings_path = root / ".cline" / "settings.json"
    if not settings_path.exists():
        return []
    # If proxy fields were the only content, remove the file
    # Otherwise, merge out proxy fields
    content = json.loads(settings_path.read_text())
    # Remove proxy-specific keys
    for key in ("apiProvider", "apiBaseUrl", "apiKey", "model"):
        content.pop(key, None)
    if not content:
        settings_path.unlink()
        return [settings_path]
    settings_path.write_text(json.dumps(content, indent=2))
    return [settings_path]
