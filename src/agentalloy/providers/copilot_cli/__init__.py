"""Copilot CLI provider — HarnessSpec registration for the standalone GitHub Copilot CLI.

Registers the ``copilot-cli`` harness in REGISTRY with:
- Protocol: OPENAI (BYOK ``COPILOT_PROVIDER_TYPE=openai`` speaks the OpenAI
  Chat Completions API)
- Capabilities: PROXY (BYOK base-URL override, localhost supported)
- env_builder: sets COPILOT_PROVIDER_* vars for the ``copilot`` binary
- install_writer: writes .copilot/.agentalloy-env with the BYOK vars

This is the standalone terminal agent (npm ``@github/copilot``); the IDE /
extension surface stays registered as the markdown-only ``github-copilot``
harness because its model traffic routes through GitHub's backend.
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
    """Build environment dict for the copilot subprocess.

    Sets the BYOK ``COPILOT_PROVIDER_*`` vars so Copilot CLI routes API calls
    through the AgentAlloy proxy. The base URL carries the per-repo
    ``/proj/<token>`` discriminator (realpath of cwd) so the proxy resolves
    this repo's phase/lifecycle — parity with codex/openclaw.
    """
    return install.build_env(port, Path.cwd())


def _install_writer(port: int, root: Path, force: bool = False) -> list[WireRecord]:
    """Install wiring for copilot-cli by writing .copilot/.agentalloy-env."""
    return install.apply_persistent_config(port, root, force)


# Register the harness in the global REGISTRY.
REGISTRY["copilot-cli"] = HarnessSpec(
    name="copilot-cli",
    binary="copilot",
    capabilities=(Capability.PROXY,),
    protocol=Protocol.OPENAI,
    env_builder=_env_builder,
    install_writer=_install_writer,
)
