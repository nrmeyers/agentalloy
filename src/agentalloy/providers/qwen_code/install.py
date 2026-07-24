"""Qwen Code install module — apply_persistent_config / install_writer.

Proxy wiring via ``~/.qwen/settings.json``. Qwen Code reads its upstream LLM
configuration from this file (modelProviders.openai[].baseUrl, model.baseUrl).
The install_writer updates those fields to point at the AgentAlloy proxy while
preserving every other setting the user already has.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, cast

from agentalloy.providers.base import WireRecord


def _sha256(content: str) -> str:
    """Compute SHA-256 hex digest of content."""
    return hashlib.sha256(content.encode()).hexdigest()


def _capture_original(path: Path) -> str | None:
    """Read and return the file's content if it exists, else None."""
    if path.exists():
        return path.read_text()
    return None


def apply_persistent_config(port: int, root: Path, force: bool = False) -> list[WireRecord]:
    """Install persistent proxy wiring for qwen-code.

    Updates ``~/.qwen/settings.json`` to point the Qwen Code LLM provider
    at the AgentAlloy proxy. Preserves all other settings.

    Args:
        port: The AgentAlloy proxy port.
        root: The repository root (unused — Qwen Code config is home-scoped).
        force: Unused — the edit is idempotent (only touches proxy URLs).

    Returns:
        List of WireRecord describing files written.
    """
    _ = root
    _ = force

    settings_path = Path.home() / ".qwen" / "settings.json"
    original_content = _capture_original(settings_path)

    # Read existing settings or start with empty dict
    data: dict[str, Any] = {}
    if settings_path.exists():
        try:
            raw = settings_path.read_text(encoding="utf-8")
            if raw.strip():
                data = cast("dict[str, Any]", json.loads(raw))
        except (json.JSONDecodeError, OSError):
            # Malformed settings — start fresh but keep original for WireRecord
            pass

    proxy_url = f"http://localhost:{port}/v1"

    # Update modelProviders.openai[] — these are the persistent provider entries
    if "modelProviders" in data and isinstance(data["modelProviders"], dict):
        providers = cast("dict[str, Any]", data["modelProviders"])
        openai_providers = providers.get("openai")
        if isinstance(openai_providers, list):
            # Update existing entries to point at the proxy
            for entry in openai_providers:
                if isinstance(entry, dict):
                    entry["baseUrl"] = proxy_url
        else:
            providers["openai"] = [
                {
                    "id": "agentalloy-proxy",
                    "name": "AgentAlloy",
                    "baseUrl": proxy_url,
                }
            ]
    else:
        data["modelProviders"] = {
            "openai": [
                {
                    "id": "agentalloy-proxy",
                    "name": "AgentAlloy",
                    "baseUrl": proxy_url,
                }
            ]
        }

    # Update top-level model.baseUrl (Qwen Code reads this for the active model)
    if "model" in data and isinstance(data["model"], dict):
        data["model"]["baseUrl"] = proxy_url
    else:
        data["model"] = {
            "baseUrl": proxy_url,
        }

    # Ensure auth type is openai
    if "security" in data and isinstance(data["security"], dict):
        auth = data["security"].get("auth")
        if isinstance(auth, dict):
            auth["selectedType"] = "openai"
    else:
        data["security"] = {
            "auth": {
                "apiKey": "local",
                "selectedType": "openai",
            }
        }

    # Write back
    content = json.dumps(data, indent=2) + "\n"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(content, encoding="utf-8")

    action = "wrote_new_file" if (original_content is None) else "replaced_file"

    print(
        f"[AgentAlloy] qwen-code wired via ~/.qwen/settings.json (baseUrl={proxy_url}). "
        "Launch ``qwen`` to route through the AgentAlloy proxy.",
        file=sys.stderr,
    )

    return [
        WireRecord(
            path=str(settings_path),
            action=action,
            content_sha256=_sha256(content),
            original_content=original_content,
            marker_key="qwen-code.settings",
        )
    ]
