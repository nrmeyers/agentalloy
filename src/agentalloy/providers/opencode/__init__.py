"""OpenCode provider — HarnessSpec registration for OpenCode CLI.

Registers the ``opencode`` harness in REGISTRY with:
- Protocol: OPENAI (via a custom ``@ai-sdk/openai-compatible`` provider —
  OpenCode's built-in openai provider speaks the Responses API, which the
  proxy does not serve)
- Capabilities: PROXY (proxy wiring via repo-local ``opencode.json``)
- env_builder: returns empty dict (OpenCode ignores ``OPENAI_API_BASE``;
  the config file is the only working vector)
- install_writer: writes/merges repo-local ``opencode.json``
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
    """Build environment dict for the opencode subprocess.

    OpenCode does not honor OpenAI base-URL env vars (verified against a live
    binary); routing comes entirely from the repo-local ``opencode.json``
    carrier, which it picks up from the launch cwd. Returns an empty dict.
    """
    return {}


def _install_writer(port: int, root: Path, force: bool = False) -> list[WireRecord]:
    """Install persistent wiring for opencode.

    Writes/merges repo-local ``opencode.json`` with the agentalloy provider
    block and default model.
    """
    return install.apply_persistent_config(port, root, force)


# Register the harness in the global REGISTRY.
REGISTRY["opencode"] = HarnessSpec(
    name="opencode",
    binary="opencode",
    capabilities=(Capability.PROXY,),
    protocol=Protocol.OPENAI,
    env_builder=_env_builder,
    install_writer=_install_writer,
)
