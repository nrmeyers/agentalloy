"""Cline install module — apply_persistent_config / install_writer.

Proxy wiring via Cline's real provider store. The previously shipped
repo-local ``.cline/settings.json`` was **inert** — Cline reads provider
config from VS Code globalState and (since CLI 2.0) the user-scoped
``~/.cline/data/settings/providers.json``; the project ``.cline/`` directory
explicitly excludes provider settings. This module writes/merges the
``openai-compatible`` provider entry into ``providers.json`` — the exact
schema ``cline auth -p openai-compatible`` produces (captured from a live
binary by the harness e2e matrix):

    providers["openai-compatible"] = {
        "settings": {provider, apiKey, model, baseUrl},
        "updatedAt": ISO-8601, "tokenSource": "manual"}
    lastUsedProvider = "openai-compatible"

User-scoped (one store per machine), so the base URL targets the proxy's bare
``/v1`` surface — a per-repo ``/proj/<token>`` in a global store would
misattribute every other repo's traffic.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, cast

from agentalloy.providers.base import WireRecord


def _sha256(content: str) -> str:
    """Compute SHA-256 hex digest of content."""
    return hashlib.sha256(content.encode()).hexdigest()


def providers_json_path(data_dir: Path | None = None) -> Path:
    """Cline's provider store (``--data-dir`` layout: <dir>/settings/providers.json;
    default user scope nests under ``~/.cline/data/``)."""
    if data_dir is not None:
        return data_dir / "settings" / "providers.json"
    return Path.home() / ".cline" / "data" / "settings" / "providers.json"


def render_providers(port: int, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    """Merge the agentalloy openai-compatible provider into a providers store."""
    store: dict[str, Any] = dict(existing or {})
    store.setdefault("version", 1)
    providers = store.get("providers")
    if not isinstance(providers, dict):
        providers = {}
    providers = cast("dict[str, Any]", dict(providers))
    providers["openai-compatible"] = {
        "settings": {
            "provider": "openai-compatible",
            "apiKey": "agentalloy",
            "model": "agentalloy-proxy",
            "baseUrl": f"http://localhost:{port}/v1",
        },
        # Match `cline auth`'s JS-style timestamp exactly (milliseconds + Z):
        # a strict parse of this field silently invalidates the whole store.
        "updatedAt": datetime.datetime.now(datetime.UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        "tokenSource": "manual",
    }
    store["providers"] = providers
    store["lastUsedProvider"] = "openai-compatible"
    return store


def apply_persistent_config(port: int, root: Path, force: bool = False) -> list[WireRecord]:
    """Install wiring for cline by merging into the user-scoped providers.json.

    Args:
        port: The AgentAlloy proxy port.
        root: The repository root (unused — cline's provider store is user-scoped).
        force: Unused — the provider entry is idempotently replaced.

    Returns:
        List of WireRecord describing files written.
    """
    _ = root, force
    path = providers_json_path()

    original_content = path.read_text(encoding="utf-8") if path.exists() else None
    existing: dict[str, Any] = {}
    if original_content is not None:
        try:
            parsed: Any = json.loads(original_content)
            if isinstance(parsed, dict):
                existing = cast("dict[str, Any]", parsed)
        except json.JSONDecodeError as exc:
            print(f"ERROR: {path} is not valid JSON", file=sys.stderr)
            print("FIX:   Fix the JSON syntax or remove the file.", file=sys.stderr)
            raise SystemExit(1) from exc

    store = render_providers(port, existing)
    content = json.dumps(store, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

    print(
        "[AgentAlloy] cline wired via ~/.cline/data/settings/providers.json "
        "(openai-compatible provider → proxy /v1; becomes the last-used provider "
        "for the VS Code extension and the cline CLI). User-scoped: cline traffic "
        "routes through the proxy's global /v1 surface.",
        file=sys.stderr,
    )

    return [
        WireRecord(
            path=str(path),
            action="wrote_new_file" if original_content is None else "injected_block",
            content_sha256=_sha256(content),
            original_content=original_content,
            marker_key="cline.providers.openai-compatible",
        )
    ]
