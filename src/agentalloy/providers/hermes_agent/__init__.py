"""Hermes Agent provider — HarnessSpec registration for Hermes Agent.

Registers the ``hermes-agent`` harness in REGISTRY with:
- Protocol: ANTHROPIC (Hermes Agent speaks the Anthropic Messages API)
- Capabilities: PROXY (proxy wiring via config file)
- env_builder: returns empty dict (Hermes Agent uses file-based config)
- install_writer: writes ~/.hermes/SOUL.md (user scope) or AGENTS.md (repo scope)
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

    Hermes Agent uses file-based config (~/.hermes/SOUL.md or AGENTS.md)
    rather than env vars. Returns an empty dict.
    """
    return {}


def _install_writer(port: int, root: Path, force: bool = False) -> list[WireRecord]:
    """Install persistent wiring for hermes-agent.

    Writes ~/.hermes/SOUL.md (user scope) or AGENTS.md (repo scope)
    with the AgentAlloy proxy configuration.
    """
    return install.apply_persistent_config(port, root, force)


# Register the harness in the global REGISTRY.
REGISTRY["hermes-agent"] = HarnessSpec(
    name="hermes-agent",
    binary="hermes",
    capabilities=(Capability.PROXY,),
    protocol=Protocol.ANTHROPIC,
    env_builder=_env_builder,
    install_writer=_install_writer,
)
