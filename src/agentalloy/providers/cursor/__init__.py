"""Cursor provider — HarnessSpec registration for Cursor IDE.

Registers the ``cursor`` harness in REGISTRY with:
- Protocol: ANTHROPIC (Cursor speaks the Anthropic Messages API via API key)
- Capabilities: MARKDOWN_ONLY (sidecar harness, markdown injection)
- env_builder: returns empty dict (Cursor uses markdown injection)
- install_writer: writes .cursor/rules/agentalloy.mdc or .cursorrules
- hook_writer: None (Cursor does not use hook-based wiring)
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
    """Build environment dict for the cursor subprocess.

    Cursor is a sidecar harness that uses markdown injection, not proxy.
    Returns an empty dict.
    """
    return {}


def _install_writer(port: int, root: Path, force: bool = False) -> list[WireRecord]:
    """Install persistent wiring for cursor.

    Writes .cursor/rules/agentalloy.mdc (dedicated) or .cursorrules (shared)
    with the AgentAlloy skill-context instruction block.
    """
    return install.apply_persistent_config(port, root, force)


def _hook_writer(port: int, root: Path) -> list[WireRecord]:
    """Hook writer for cursor — not applicable for markdown injection."""
    return []


# Register the harness in the global REGISTRY.
REGISTRY["cursor"] = HarnessSpec(
    name="cursor",
    binary="cursor",
    capabilities=(Capability.MARKDOWN_ONLY,),
    protocol=Protocol.ANTHROPIC,
    env_builder=_env_builder,
    hook_writer=_hook_writer,
    install_writer=_install_writer,
)
