"""Aider provider — HarnessSpec registration for Aider CLI.

Registers the ``aider`` harness in REGISTRY with:
- Protocol: OPENAI (Aider speaks the OpenAI Chat Completions API)
- Capabilities: PROXY (proxy wiring via .aider.conf.yml)
- env_builder: returns empty dict (Aider uses file-based config, not env vars)
- install_writer: writes .agentalloy-aider-instructions.md + .aider.conf.yml
- hook_writer: None (Aider does not use hook-based wiring)
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
    """Build environment dict for the aider subprocess.

    Aider uses file-based config (.aider.conf.yml) rather than env vars.
    Returns an empty dict.
    """
    return {}


def _install_writer(port: int, root: Path, force: bool = False) -> list[WireRecord]:
    """Install persistent wiring for aider.

    Writes .agentalloy-aider-instructions.md and updates .aider.conf.yml.
    """
    return install.apply_persistent_config(port, root, force)


def _hook_writer(port: int, root: Path) -> list[WireRecord]:
    """Hook writer for aider — not applicable for file-based config."""
    return []


# Register the harness in the global REGISTRY.
REGISTRY["aider"] = HarnessSpec(
    name="aider",
    binary="aider",
    capabilities=(Capability.PROXY,),
    protocol=Protocol.OPENAI,
    env_builder=_env_builder,
    hook_writer=_hook_writer,
    install_writer=_install_writer,
)
