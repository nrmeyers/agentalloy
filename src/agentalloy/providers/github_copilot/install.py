"""GitHub Copilot (VS Code) install module — apply_persistent_config / install_writer.

Two carriers:

1. **Ambient instructions** — the sidecar block in
   ``.github/copilot-instructions.md`` (written via the shared
   ``_wire_proxy_instruction`` / watch-regenerator path, so wire-time seed and
   watcher refresh target one identical block).
2. **BYOK proxy carrier** — a ``customendpoint`` provider group in VS Code's
   user-profile ``chatLanguageModels.json`` (BYOK GA Apr 2026; works without a
   GitHub sign-in since VS Code 1.122). ``url`` is the FULL endpoint path and
   ``apiType`` selects the wire — we use the proxy's bare
   ``/v1/chat/completions`` (user-scoped store → bare surface, same rule as
   openclaw/cline). ``toolCalling: true`` is required for the model to appear
   in agent mode.

Caveats encoded in the wire-time guidance: VS Code may need a restart to
refresh the model picker; Copilot Business/Enterprise policy can disable BYOK
entirely; this carrier is NOT machine-verified (no headless VS Code) — the
manual smoke checklist covers it.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, cast

from agentalloy.providers.base import WireRecord

PROVIDER_NAME = "AgentAlloy"


def _sha256(content: str) -> str:
    """Compute SHA-256 hex digest of content."""
    return hashlib.sha256(content.encode()).hexdigest()


def vscode_user_dir() -> Path | None:
    """The stable VS Code user-profile dir for this platform, if it exists.

    Only the stable channel ("Code") is targeted; Insiders/VSCodium users can
    copy the provider group manually (documented in the harness catalog).
    """
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "Code" / "User"
    elif os.name == "nt":
        # An unset APPDATA yields a relative path that never exists — the
        # is_dir() check below turns that into a clean skip.
        base = Path(os.environ.get("APPDATA", "")) / "Code" / "User"
    else:
        base = Path.home() / ".config" / "Code" / "User"
    return base if base.is_dir() else None


def render_provider_group(port: int) -> dict[str, Any]:
    """The AgentAlloy customendpoint provider group for chatLanguageModels.json."""
    return {
        "name": PROVIDER_NAME,
        "vendor": "customendpoint",
        "apiKey": "agentalloy",
        "apiType": "chat-completions",
        "models": [
            {
                "id": "agentalloy-proxy",
                "name": "AgentAlloy Proxy",
                "url": f"http://localhost:{port}/v1/chat/completions",
                "toolCalling": True,
                "maxInputTokens": 200000,
                "maxOutputTokens": 16000,
            }
        ],
    }


def apply_byok_config(port: int) -> list[WireRecord]:
    """Merge the AgentAlloy provider group into VS Code's chatLanguageModels.json.

    Returns an empty list (with guidance on stderr) when no VS Code user
    profile exists on this machine — the instructions-file carrier still
    applies, so wiring must not fail outright.
    """
    user_dir = vscode_user_dir()
    if user_dir is None:
        print(
            "[AgentAlloy] github-copilot: no VS Code user profile found — skipped the "
            "BYOK proxy carrier (chatLanguageModels.json). The instructions-file "
            "sidecar is still wired; re-run `agentalloy wire --harness github-copilot` "
            "after installing VS Code to add the proxy carrier.",
            file=sys.stderr,
        )
        return []

    path = user_dir / "chatLanguageModels.json"
    original_content = path.read_text(encoding="utf-8") if path.exists() else None
    groups: list[Any] = []
    if original_content is not None:
        try:
            parsed: Any = json.loads(original_content)
            if isinstance(parsed, list):
                groups = cast("list[Any]", parsed)
        except json.JSONDecodeError as exc:
            print(f"ERROR: {path} is not valid JSON", file=sys.stderr)
            print("FIX:   Fix the JSON syntax or remove the file.", file=sys.stderr)
            raise SystemExit(1) from exc

    groups = [g for g in groups if not (isinstance(g, dict) and g.get("name") == PROVIDER_NAME)]
    groups.append(render_provider_group(port))

    content = json.dumps(groups, indent=2) + "\n"
    path.write_text(content, encoding="utf-8")

    print(
        "[AgentAlloy] github-copilot BYOK carrier written: VS Code "
        "chatLanguageModels.json now carries the 'AgentAlloy' custom-endpoint "
        "provider (model 'AgentAlloy Proxy', agent-mode capable). Restart VS Code "
        "and pick it via 'Chat: Manage Language Models'. Note: Copilot "
        "Business/Enterprise policy can disable BYOK models.",
        file=sys.stderr,
    )

    return [
        WireRecord(
            path=str(path),
            action="wrote_new_file" if original_content is None else "injected_block",
            content_sha256=_sha256(content),
            original_content=original_content,
            marker_key="github-copilot.byok.agentalloy",
        )
    ]


def apply_persistent_config(port: int, root: Path, force: bool = False) -> list[WireRecord]:
    """Install both github-copilot carriers.

    Writes the ambient instructions block via the shared sidecar writer
    (identical file/markers to the watch regenerator) and merges the BYOK
    provider group into VS Code's user-profile chatLanguageModels.json.
    """
    # Lazy import: wire_harness imports the provider registry at module load,
    # so a top-level import here would be circular.
    from agentalloy.install.subcommands.wire_harness import (
        _wire_proxy_instruction,  # pyright: ignore[reportPrivateUsage]
    )

    _ = force
    records = [
        WireRecord.from_dict(r)
        for r in _wire_proxy_instruction("github-copilot", port, root, "repo")
    ]
    records.extend(apply_byok_config(port))
    return records
