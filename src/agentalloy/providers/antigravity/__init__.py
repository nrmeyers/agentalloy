"""Antigravity CLI provider — HarnessSpec registration for Google Antigravity CLI.

Antigravity CLI is the current name of what was previously Google Gemini CLI;
the ``gemini-cli`` key is kept in REGISTRY as a deprecated alias so existing
wiring, state records, and muscle memory keep working.

Registers the ``antigravity`` harness in REGISTRY with:
- Protocol: EITHER (Antigravity CLI can use either Anthropic or OpenAI protocol)
- Capabilities: MARKDOWN_ONLY (sidecar harness, markdown injection)
- env_builder: returns empty dict (Antigravity CLI uses markdown injection)
- install_writer: writes GEMINI.md with sentinel-bounded block (Antigravity
  still reads GEMINI.md as its instruction file)
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
    """Build environment dict for the antigravity subprocess.

    Antigravity CLI is a sidecar harness that uses markdown injection, not
    proxy. Returns an empty dict.
    """
    return {}


def _install_writer(port: int, root: Path, force: bool = False) -> list[WireRecord]:
    """Install persistent wiring for antigravity.

    Writes GEMINI.md with a sentinel-bounded block containing the
    AgentAlloy skill-context prose for Antigravity CLI.
    """
    return install.apply_persistent_config(port, root, force)


# Register the harness in the global REGISTRY.
REGISTRY["antigravity"] = HarnessSpec(
    name="antigravity",
    binary="antigravity",
    capabilities=(Capability.MARKDOWN_ONLY,),
    protocol=Protocol.EITHER,
    env_builder=_env_builder,
    install_writer=_install_writer,
)

# Deprecated alias — Antigravity CLI was formerly Gemini CLI. Same spec object,
# so `agentalloy add gemini-cli` and old state records resolve identically.
REGISTRY["gemini-cli"] = REGISTRY["antigravity"]
