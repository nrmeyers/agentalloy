"""Cline provider — HarnessSpec registration for Cline VS Code extension.

Registers the ``cline`` harness in REGISTRY with:
- Protocol: OPENAI (Cline speaks the OpenAI Chat Completions API)
- Capabilities: PROXY (proxy wiring via .cline/settings.json)
- env_builder: returns empty dict (Cline uses file-based config)
- install_writer: writes .cline/settings.json with proxy API fields
- hook_writer: None (Cline does not use hook-based wiring)
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
    """Build environment dict for the cline subprocess.

    Cline uses file-based config (.cline/settings.json) rather than env vars.
    Returns an empty dict.
    """
    return {}


def _install_writer(port: int, root: Path, force: bool = False) -> list[WireRecord]:
    """Install persistent wiring for cline.

    Writes .cline/settings.json with proxy API fields.
    """
    return install.apply_persistent_config(port, root, force)


def _hook_writer(port: int, root: Path) -> list[WireRecord]:
    """Hook writer for cline — not applicable for file-based config."""
    return []


# Register the harness in the global REGISTRY.
REGISTRY["cline"] = HarnessSpec(
    name="cline",
    binary="cline",
    capabilities=(Capability.PROXY,),
    protocol=Protocol.OPENAI,
    env_builder=_env_builder,
    hook_writer=_hook_writer,
    install_writer=_install_writer,
)
