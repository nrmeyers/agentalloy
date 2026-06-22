"""Gemini CLI provider — HarnessSpec registration for Google Gemini CLI.

Registers the ``gemini-cli`` harness in REGISTRY with:
- Protocol: EITHER (Gemini CLI can use either Anthropic or OpenAI protocol)
- Capabilities: MARKDOWN_ONLY (sidecar harness, markdown injection)
- env_builder: returns empty dict (Gemini CLI uses markdown injection)
- install_writer: writes GEMINI.md with sentinel-bounded block
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
    """Build environment dict for the gemini-cli subprocess.

    Gemini CLI is a sidecar harness that uses markdown injection, not proxy.
    Returns an empty dict.
    """
    return {}


def _install_writer(port: int, root: Path, force: bool = False) -> list[WireRecord]:
    """Install persistent wiring for gemini-cli.

    Writes GEMINI.md with a sentinel-bounded block containing the
    AgentAlloy skill-context prose for Gemini CLI.
    """
    return install.apply_persistent_config(port, root, force)


# Register the harness in the global REGISTRY.
REGISTRY["gemini-cli"] = HarnessSpec(
    name="gemini-cli",
    binary="gemini",
    capabilities=(Capability.MARKDOWN_ONLY,),
    protocol=Protocol.EITHER,
    env_builder=_env_builder,
    install_writer=_install_writer,
)
