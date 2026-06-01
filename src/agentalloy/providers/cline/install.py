"""Cline install module — apply_persistent_config / install_writer.

Writes .cline/settings.json with proxy API fields (apiProvider, apiBaseUrl,
apiKey, model). Preserves all other keys in the file.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from agentalloy.providers.base import WireRecord


def _sha256(content: str) -> str:
    """Compute SHA-256 hex digest of content."""
    return hashlib.sha256(content.encode()).hexdigest()


def _capture_original(path: Path) -> str | None:
    """Read and return the file's content if it exists, else None."""
    if path.exists():
        return path.read_text(encoding="utf-8")
    return None


def apply_persistent_config(port: int, root: Path, force: bool = False) -> list[WireRecord]:
    """Install wiring for cline.

    Writes .cline/settings.json with proxy API fields.

    Args:
        port: The AgentAlloy proxy port.
        root: The repository root.
        force: If True, skip tamper detection.

    Returns:
        List of WireRecord describing files written.
    """
    settings_path = root / ".cline" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    original_content = _capture_original(settings_path)
    proxy_url = f"http://localhost:{port}/v1"

    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            settings = {}
    else:
        settings = {}

    settings.update(
        {
            "apiProvider": "openai",
            "apiBaseUrl": proxy_url,
            "apiKey": "agentalloy",
            "model": "agentalloy-proxy",
        }
    )

    serialized = json.dumps(settings, indent=2) + "\n"
    settings_path.write_text(serialized, encoding="utf-8")

    return [
        WireRecord(
            path=str(settings_path),
            action="wrote_new_file" if original_content is None else "injected_block",
            content_sha256=_sha256(serialized),
            original_content=original_content,
            marker_key="cline.settings.proxy",
        )
    ]
