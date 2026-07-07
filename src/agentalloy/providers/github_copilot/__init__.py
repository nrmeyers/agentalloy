"""GitHub Copilot (VS Code) provider — HarnessSpec registration.

Registers the ``github-copilot`` harness in REGISTRY with:
- Protocol: OPENAI (BYOK custom endpoint speaks Chat Completions)
- Capabilities: PROXY + MARKDOWN_ONLY — dual-carrier: the BYOK
  ``chatLanguageModels.json`` provider group gives true per-turn proxy
  interception (agent mode included), while the ``.github/copilot-instructions.md``
  sidecar block stays as ambient context (and covers policy-disabled BYOK).
- env_builder: returns empty dict (both carriers are file-based)
- install_writer: writes both carriers (see install.py)

The standalone Copilot CLI is the separate ``copilot-cli`` harness.
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
    """Build environment dict for the github-copilot subprocess.

    Both carriers (VS Code chatLanguageModels.json + instructions markdown)
    are file-based. Returns an empty dict.
    """
    _ = port
    return {}


def _install_writer(port: int, root: Path, force: bool = False) -> list[WireRecord]:
    """Install persistent wiring for github-copilot (both carriers)."""
    return install.apply_persistent_config(port, root, force)


# Register the harness in the global REGISTRY.
REGISTRY["github-copilot"] = HarnessSpec(
    name="github-copilot",
    binary="gh copilot",
    capabilities=(Capability.PROXY, Capability.MARKDOWN_ONLY),
    protocol=Protocol.OPENAI,
    env_builder=_env_builder,
    install_writer=_install_writer,
)
