"""Uninstall logic for proxy configs.

Functions to reverse each proxy wiring operation. Each uses the same sentinel comments
as the corresponding wire function for bounded removal.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _remove_sentinel_block(content: str) -> str:
    """Remove content between agentalloy sentinels.

    Handles both raw HTML-style sentinels (<!-- BEGIN ... -->) and
    commented-out variants (# <!-- BEGIN ... -->) used by YAML/shell files.
    Operates on whole lines so leading '#' fragments are not left behind.

    Returns the original content unchanged if no sentinels are found.
    """
    lines = content.split("\n")
    result: list[str] = []
    skip = False
    found_sentinel = False
    sentinel_begin_raw = "<!-- BEGIN agentalloy install -->"
    sentinel_end_raw = "<!-- END agentalloy install -->"
    sentinel_begin_commented = "# " + sentinel_begin_raw
    sentinel_end_commented = "# " + sentinel_end_raw

    i = 0
    while i < len(lines):
        line = lines[i]
        # Check for begin sentinel (raw or commented)
        if sentinel_begin_raw in line or sentinel_begin_commented in line:
            skip = True
            found_sentinel = True
            i += 1
            continue
        # Check for end sentinel (raw or commented)
        if skip and (sentinel_end_raw in line or sentinel_end_commented in line):
            skip = False
            i += 1
            # Skip trailing blank line after end sentinel
            if i < len(lines) and lines[i].strip() == "":
                i += 1
            continue
        if not skip:
            result.append(line)
        i += 1

    # Only clean up blank lines if we actually removed a sentinel block
    if not found_sentinel:
        return content

    cleaned: list[str] = []
    blank_count = 0
    for line in result:
        if line.strip() == "":
            blank_count += 1
            if blank_count < 3:
                cleaned.append(line)
        else:
            blank_count = 0
            cleaned.append(line)

    return "\n".join(cleaned)


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
        print("Remove the source line from .bashrc/.zshrc manually:", file=sys.stderr)
        print("  # AgentAlloy: claude-code proxy env", file=sys.stderr)
        return [env_path]
    return []


def _unwire_proxy_cline(root: Path) -> list[Path]:
    """Remove cline settings file."""
    settings_path = root / ".cline" / "settings.json"
    if not settings_path.exists():
        return []
    # If proxy fields were the only content, remove the file
    # Otherwise, merge out proxy fields
    try:
        content = json.loads(settings_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(
            f"WARNING: {settings_path} could not be parsed ({e}) — skipping cline cleanup.",
            file=sys.stderr,
        )
        return []

    # Only remove keys if they match AgentAlloy proxy values to avoid
    # removing user's own settings that happen to use the same keys.
    removed_any = False

    for key, val in list(content.items()):
        if (
            key == "apiProvider"
            and val == "openai"
            or key == "apiBaseUrl"
            and isinstance(val, str)
            and "localhost" in val
            or key == "apiKey"
            and val in ("***", "agentalloy")
            or key == "model"
            and val == "agentalloy-proxy"
        ):
            content.pop(key)
            removed_any = True

    if not removed_any:
        # No proxy keys found — nothing to do
        return []

    if not content:
        settings_path.unlink()
        return [settings_path]
    settings_path.write_text(json.dumps(content, indent=2))
    return [settings_path]
