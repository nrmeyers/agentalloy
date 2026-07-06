"""OpenClaw install module — apply_persistent_config / install_writer.

Proxy wiring for OpenClaw (npm ``openclaw``, the Clawdbot/Moltbot lineage
personal-agent runtime). The previously shipped ``~/.openclaw/plugins.json``
"proxy plugin" entry was never OpenClaw schema, and OpenClaw does not honor
``OPENAI_BASE_URL`` — the real vector (verified live by the harness e2e
matrix) is a **custom model provider** in ``~/.openclaw/openclaw.json``:

    models.providers.agentalloy = {baseUrl, api: "openai-completions",
                                   apiKey, models: [agentalloy-proxy]}
    agents.defaults.model.primary = "agentalloy/agentalloy-proxy"

OpenClaw is a user-scoped assistant (one gateway per machine), so the config
is user-scoped and points at the proxy's bare ``/v1`` surface — a per-repo
``/proj/<token>`` in a global config would silently misattribute every other
repo's traffic.
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


def _load_config(path: Path) -> dict[str, Any]:
    """Load existing openclaw.json or return an empty structure. Fail loud on
    unparseable content — overwriting a broken-but-real config would silently
    destroy it."""
    if not path.exists():
        return {}
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"ERROR: {path} is not valid JSON", file=sys.stderr)
        print("FIX:   Fix the JSON syntax or remove the file.", file=sys.stderr)
        raise SystemExit(1) from exc
    if not isinstance(data, dict):
        print(f"ERROR: {path} is not a JSON object", file=sys.stderr)
        print("FIX:   Fix the file (expected an object) or remove it.", file=sys.stderr)
        raise SystemExit(1)
    return cast("dict[str, Any]", data)


def render_config(port: int, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    """Merge the agentalloy provider + default model into an openclaw config."""
    config: dict[str, Any] = dict(existing or {})

    models = config.get("models")
    if not isinstance(models, dict):
        models = {}
    models = cast("dict[str, Any]", dict(models))
    providers = models.get("providers")
    if not isinstance(providers, dict):
        providers = {}
    providers = cast("dict[str, Any]", dict(providers))
    providers["agentalloy"] = {
        "baseUrl": f"http://localhost:{port}/v1",
        "api": "openai-completions",
        "apiKey": "agentalloy",
        "models": [
            {
                "id": "agentalloy-proxy",
                "name": "AgentAlloy Proxy",
                "contextWindow": 128000,
                "maxTokens": 8192,
            }
        ],
    }
    models["providers"] = providers
    config["models"] = models

    agents = config.get("agents")
    if not isinstance(agents, dict):
        agents = {}
    agents = cast("dict[str, Any]", dict(agents))
    defaults = agents.get("defaults")
    if not isinstance(defaults, dict):
        defaults = {}
    defaults = cast("dict[str, Any]", dict(defaults))
    model = defaults.get("model")
    if not isinstance(model, dict):
        model = {}
    model = cast("dict[str, Any]", dict(model))
    model["primary"] = "agentalloy/agentalloy-proxy"
    defaults["model"] = model
    agents["defaults"] = defaults
    config["agents"] = agents
    return config


def apply_persistent_config(port: int, root: Path, force: bool = False) -> list[WireRecord]:
    """Install wiring for openclaw by merging into ``~/.openclaw/openclaw.json``.

    Args:
        port: The AgentAlloy proxy port.
        root: The repository root (unused — openclaw is user-scoped).
        force: Unused — the provider entry is idempotently replaced.

    Returns:
        List of WireRecord describing files written.
    """
    _ = root, force
    config_path = Path.home() / ".openclaw" / "openclaw.json"

    original_content = config_path.read_text(encoding="utf-8") if config_path.exists() else None
    config = render_config(port, _load_config(config_path))
    content = json.dumps(config, indent=2) + "\n"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(content, encoding="utf-8")

    print(
        "[AgentAlloy] openclaw wired via ~/.openclaw/openclaw.json "
        "(models.providers.agentalloy, default model agentalloy/agentalloy-proxy). "
        "User-scoped: openclaw traffic routes through the proxy's global /v1 surface. "
        "Restart the openclaw gateway (`openclaw gateway restart`) to pick it up.",
        file=sys.stderr,
    )

    return [
        WireRecord(
            path=str(config_path),
            action="wrote_new_file" if original_content is None else "injected_block",
            content_sha256=_sha256(content),
            original_content=original_content,
            marker_key="openclaw.models.providers.agentalloy",
        )
    ]
