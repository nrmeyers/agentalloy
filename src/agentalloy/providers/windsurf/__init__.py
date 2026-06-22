"""Windsurf provider — HarnessSpec registration for Windsurf IDE.

Registers the ``windsurf`` harness in REGISTRY with:
- Protocol: ANTHROPIC (Windsurf speaks the Anthropic Messages API)
- Capabilities: MARKDOWN_ONLY (sidecar harness, markdown injection)
- env_builder: returns empty dict (Windsurf uses markdown injection)
- install_writer: writes .windsurf/rules/agentalloy.md or .windsurfrules
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
    """Build environment dict for the windsurf subprocess.

    Windsurf is a sidecar harness that uses markdown injection, not proxy.
    Returns an empty dict.
    """
    return {}


def _install_writer(port: int, root: Path, force: bool = False) -> list[WireRecord]:
    """Install persistent wiring for windsurf.

    Writes .windsurf/rules/agentalloy.md (dedicated) or .windsurfrules (shared)
    with the AgentAlloy skill-context instruction block.
    """
    return install.apply_persistent_config(port, root, force)


# Register the harness in the global REGISTRY.
REGISTRY["windsurf"] = HarnessSpec(
    name="windsurf",
    binary="windsurf",
    capabilities=(Capability.MARKDOWN_ONLY,),
    protocol=Protocol.ANTHROPIC,
    env_builder=_env_builder,
    install_writer=_install_writer,
)
