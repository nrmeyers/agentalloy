"""Uninstall logic for proxy configs.

Functions to reverse each proxy wiring operation. Each uses the same sentinel comments
as the corresponding wire function for bounded removal.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def _remove_sentinel_block(content: str) -> str:
    """Remove content between agentalloy sentinels.

    Handles both raw HTML-style sentinels (<!-- BEGIN ... -->) and
    commented-out variants (# <!-- BEGIN ... -->) used by YAML/shell files.
    Operates on whole lines so leading '#' fragments are not left behind.

    If the END marker appears before the BEGIN marker, the content is
    returned unchanged (no valid block to remove).

    Returns the original content unchanged if no sentinels are found.
    """
    sentinel_begin_raw = "<!-- BEGIN agentalloy install -->"
    sentinel_end_raw = "<!-- END agentalloy install -->"
    sentinel_begin_commented = "# " + sentinel_begin_raw
    sentinel_end_commented = "# " + sentinel_end_raw

    # Validate order: find first occurrence of each sentinel (raw or commented)
    # and ensure END does not appear before BEGIN. If inverted, return content
    # unchanged (no valid block to remove).
    first_begin = len(content)
    first_end = len(content)

    for variant in (sentinel_begin_raw, sentinel_begin_commented):
        idx = content.find(variant)
        if idx != -1 and idx < first_begin:
            first_begin = idx

    for variant in (sentinel_end_raw, sentinel_end_commented):
        idx = content.find(variant)
        if idx != -1 and idx < first_end:
            first_end = idx

    if first_begin < len(content) and first_end < len(content) and first_end < first_begin:
        # Inverted order — no valid block to remove; return unchanged.
        return content

    lines = content.split("\n")
    result: list[str] = []
    skip = False
    found_sentinel = False

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
    new_content = _remove_sentinel_block(content)
    removed: list[Path] = []
    if new_content != content:
        conf_path.write_text(new_content)
        removed.append(conf_path)
    # Also remove instructions file if it exists (legacy installs created it)
    instr_path = root / ".agentalloy-aider-instructions.md"
    if instr_path.exists():
        instr_path.unlink()
        removed.append(instr_path)
    return removed


def _unwire_proxy_hermes_agent(scope: str, root: Path) -> list[Path]:
    """Remove hermes-agent proxy config from config.yaml."""
    config_path = Path.home() / ".hermes" / "config.yaml" if scope == "user" else root / "AGENTS.md"
    if not config_path.exists():
        return []
    content = config_path.read_text()
    new_content = _remove_sentinel_block(content)
    if new_content != content:
        config_path.write_text(new_content)
        return [config_path]
    return []


def _unwire_proxy_opencode(root: Path) -> list[Path]:
    """Remove opencode proxy env file."""
    env_path = root / ".opencode" / ".agentalloy-env"
    prompt_path = root / ".opencode" / "system-prompt.md"
    removed: list[Path] = []  # type: ignore[reportUnknownVariableType]
    if env_path.exists():
        env_path.unlink()
        removed.append(env_path)
    if prompt_path.exists():
        content = prompt_path.read_text()
        new_content = _remove_sentinel_block(content)
        if new_content != content:
            if new_content.strip():
                prompt_path.write_text(new_content)
            else:
                prompt_path.unlink()
            removed.append(prompt_path)
    return removed


def _unwire_proxy_claude_code(root: Path) -> list[Path]:
    """Remove the AgentAlloy sentinel block from the claude-code env file (delete it if empty)."""
    env_path = Path.home() / ".agentalloy" / "claude-code-env.sh"
    if not env_path.exists():
        return []
    content = env_path.read_text()
    new_content = _remove_sentinel_block(content)
    if new_content != content:
        if new_content.strip():
            env_path.write_text(new_content)
        else:
            env_path.unlink()
        print(
            "Remove any line sourcing the AgentAlloy claude-code env file from your shell profile (.bashrc/.zshrc):",
            file=sys.stderr,
        )
        print(f"  source {env_path}", file=sys.stderr)
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


def _unwire_claude_code_hooks_settings_json() -> list[dict[str, Any]]:
    """Remove AgentAlloy hook entries from ~/.claude/settings.json.

    The legacy install path may have written hook-related entries into
    settings.json. This function removes them using sentinel markers
    for safe, bounded cleanup.

    Returns a list of dicts describing what was removed.
    """
    settings_path = Path.home() / ".claude" / "settings.json"
    removed: list[dict[str, Any]] = []

    if not settings_path.exists():
        return removed

    try:
        content = settings_path.read_text()
        data = json.loads(content)
    except (json.JSONDecodeError, OSError):
        return removed

    # Sentinel-bounded removal for settings.json
    sentinel_begin = "# <!-- BEGIN agentalloy install -->"
    sentinel_end = "# <!-- END agentalloy install -->"

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
            return removed
        except json.JSONDecodeError:
            pass

    # Key-based removal (fallback) — remove hooks-related keys
    keys_to_remove: list[str] = []
    for key in data:
        if key.startswith("hooks") or key == "claude_code_hooks":
            keys_to_remove.append(key)

    for key in keys_to_remove:
        del data[key]
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
