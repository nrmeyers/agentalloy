"""Hermes Agent provider — HarnessSpec registration for Hermes Agent.

Registers the ``hermes-agent`` harness in REGISTRY with:
- Protocol: ANTHROPIC (Hermes Agent speaks the Anthropic Messages API)
- Capabilities: PROXY (proxy wiring via config file)
- env_builder: returns empty dict (Hermes Agent uses file-based config)
- install_writer: writes ~/.hermes/config.yaml (user scope) or AGENTS.md (repo scope)
- hook_writer: None (Hermes Agent does not use hook-based wiring)
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
    """Build environment dict for the hermes-agent subprocess.

    Hermes Agent uses file-based config (~/.hermes/config.yaml) rather than env vars.
    Returns an empty dict.
    """
    return {}


def _install_writer(port: int, root: Path, force: bool = False) -> list[WireRecord]:
    """Install persistent wiring for hermes-agent.

    Writes ~/.hermes/config.yaml (user scope) or AGENTS.md (repo scope)
    with the AgentAlloy proxy configuration.
    """
    return install.apply_persistent_config(port, root, force)


def _hook_writer(port: int, root: Path) -> list[WireRecord]:
    """Hook writer for hermes-agent — not applicable for file-based config."""
    return []


# Register the harness in the global REGISTRY.
REGISTRY["hermes-agent"] = HarnessSpec(
    name="hermes-agent",
    binary="hermes",
    capabilities=(Capability.PROXY,),
    protocol=Protocol.ANTHROPIC,
    env_builder=_env_builder,
    hook_writer=_hook_writer,
    install_writer=_install_writer,
)
