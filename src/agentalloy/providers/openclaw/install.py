"""Openclaw install module — apply_persistent_config / install_writer for Openclaw.

Writes ~/.openclaw/plugins.json with an agentalloy plugin entry pointing
to the AgentAlloy proxy.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from agentalloy.providers.base import WireRecord


def _sha256(content: str) -> str:
    """Compute SHA-256 hex digest of content."""
    return hashlib.sha256(content.encode()).hexdigest()


def _capture_original(path: Path) -> str | None:
    """Read and return the file's content if it exists, else None."""
    if path.exists():
        return path.read_text(encoding="utf-8")
    return None


def _load_plugins(path: Path) -> dict[str, Any]:
    """Load existing plugins.json or return empty structure."""
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError) as exc:
            # Fail loud instead of returning {} — otherwise _save_plugins would
            # overwrite the user's existing (but unparseable) config with a fresh
            # structure, silently destroying it. Matches the cline adapter.
            print(f"ERROR: {path} is not valid JSON", file=sys.stderr)
            print("FIX:   Fix the JSON syntax or remove the file.", file=sys.stderr)
            raise SystemExit(1) from exc
        if not isinstance(data, dict):
            print(f"ERROR: {path} is not a JSON object", file=sys.stderr)
            print("FIX:   Fix the file (expected an object) or remove it.", file=sys.stderr)
            raise SystemExit(1)
        return data
    return {}


def _save_plugins(path: Path, plugins: dict[str, Any]) -> None:
    """Write plugins.json with proper formatting."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plugins, indent=2) + "\n", encoding="utf-8")


def apply_persistent_config(port: int, root: Path, force: bool = False) -> list[WireRecord]:
    """Install wiring for openclaw by writing ~/.openclaw/plugins.json.

    Creates a JSON plugin config with an agentalloy plugin entry pointing
    to the AgentAlloy proxy.

    The plugins.json structure:
    {
        "plugins": {
            "agentalloy": {
                "enabled": true,
                "type": "proxy",
                "baseUrl": "http://localhost:{port}/v1",
                "apiKey": "agentalloy"
            }
        }
    }

    Args:
        port: The AgentAlloy proxy port.
        root: The repository root (used for path resolution).
        force: If True, skip tamper detection.

    Returns:
        List of WireRecord describing files written.
    """
    config_path = Path.home() / ".openclaw" / "plugins.json"

    # Tokenless on purpose: this is a USER-scoped config (one ~/.openclaw/plugins.json
    # for every repo), so it cannot carry a per-repo /proj/<token>. Per-repo
    # resolution comes from the env_builder instead (it bakes encode_proj_token of
    # the launch cwd into OPENAI_BASE_URL). A direct openclaw launch relying solely
    # on this file is not repo-disambiguated.
    proxy_url = f"http://localhost:{port}/v1"

    # Build the plugin entry
    plugin_entry = {
        "enabled": True,
        "type": "proxy",
        "baseUrl": proxy_url,
        "apiKey": "agentalloy",
    }

    # Load existing plugins
    original_content = _capture_original(config_path)
    plugins = _load_plugins(config_path)

    if "plugins" not in plugins:
        plugins["plugins"] = {}

    # Add/update the agentalloy plugin
    plugins["plugins"]["agentalloy"] = plugin_entry

    content = json.dumps(plugins, indent=2) + "\n"
    content_sha = _sha256(content)

    _save_plugins(config_path, plugins)

    return [
        WireRecord(
            path=str(config_path),
            action="wrote_new_file" if original_content is None else "injected_block",
            content_sha256=content_sha,
            original_content=original_content,
            marker_key="openclaw.plugins.agentalloy",
        )
    ]
