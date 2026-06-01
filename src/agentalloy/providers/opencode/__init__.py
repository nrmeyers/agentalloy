"""OpenCode provider — HarnessSpec registration for OpenCode CLI.

Registers the ``opencode`` harness in REGISTRY with:
- Protocol: OPENAI (OpenCode speaks the OpenAI Chat Completions API)
- Capabilities: PROXY (proxy wiring via env file + system prompt)
- env_builder: sets OPENAI_API_BASE and OPENAI_API_KEY
- install_writer: writes .opencode/.agentalloy-env + .opencode/system-prompt.md
- hook_writer: None (OpenCode does not use hook-based wiring)
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

    Sets OPENAI_API_BASE and OPENAI_API_KEY so opencode routes API calls
    through the AgentAlloy proxy.
    """
    return {
        "OPENAI_API_BASE": f"http://localhost:{port}/v1",
        "OPENAI_API_KEY": "agentalloy",
    }


def _install_writer(port: int, root: Path, force: bool = False) -> list[WireRecord]:
    """Install persistent wiring for opencode.

    Writes .opencode/.agentalloy-env and .opencode/system-prompt.md.
    """
    return install.apply_persistent_config(port, root, force)


def _hook_writer(port: int, root: Path) -> list[WireRecord]:
    """Hook writer for opencode — not applicable for file-based config."""
    return []


# Register the harness in the global REGISTRY.
REGISTRY["opencode"] = HarnessSpec(
    name="opencode",
    binary="opencode",
    capabilities=(Capability.PROXY,),
    protocol=Protocol.OPENAI,
    env_builder=_env_builder,
    hook_writer=_hook_writer,
    install_writer=_install_writer,
)
