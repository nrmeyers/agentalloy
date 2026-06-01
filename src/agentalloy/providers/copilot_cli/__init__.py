"""Copilot CLI provider for AgentAlloy.

Registers the ``github-copilot`` harness in REGISTRY with:
- Protocol: OPENAI (GitHub Copilot CLI uses OpenAI-compatible API)
- Capabilities: MARKDOWN_ONLY (sidecar harness, markdown injection)
- env_builder: returns empty dict (no env vars needed for markdown-only harness)
- install_writer: writes .github/copilot-instructions.md with sentinel-bounded block
- hook_writer: None (not applicable for markdown injection)
"""

from __future__ import annotations

from pathlib import Path

from agentalloy.providers import REGISTRY
from agentalloy.providers.base import (
    Capability,
    HarnessSpec,
    Protocol,
    WireRecord,
)

from . import install


def _env_builder(port: int) -> dict[str, str]:
    """Build environment dict for the copilot CLI subprocess.

    GitHub Copilot CLI is a sidecar harness that uses markdown injection, not proxy.
    Returns an empty dict.
    """
    return {}


def _install_writer(port: int, root: Path, force: bool = False) -> list[WireRecord]:
    """Install persistent wiring for copilot CLI."""
    return install.apply_persistent_config(port, root, force)


def _hook_writer(port: int, root: Path) -> list[WireRecord]:
    """Hook writer for copilot CLI — not applicable for markdown injection."""
    return []


# Register the harness in the global REGISTRY.
REGISTRY["github-copilot"] = HarnessSpec(
    name="github-copilot",
    binary="gh copilot",
    capabilities=(Capability.MARKDOWN_ONLY,),
    protocol=Protocol.OPENAI,
    env_builder=_env_builder,
    hook_writer=_hook_writer,
    install_writer=_install_writer,
)
