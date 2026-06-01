"""Codex provider — HarnessSpec registration for OpenAI Codex CLI.

Registers the ``codex`` harness in REGISTRY with:
- Protocol: OPENAI (Codex speaks the OpenAI Chat Completions API)
- Capabilities: PROXY (proxy wiring via env vars)
- env_builder: sets OPENAI_BASE_URL for the codex binary
- install_writer: writes ~/.codex/config.toml with apiBaseUrl sentinel
- hook_writer: None (Codex does not use Claude Code hooks)
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
    """Build environment dict for the codex subprocess.

    Sets OPENAI_BASE_URL so codex routes API calls through the AgentAlloy proxy.
    """
    return {
        "OPENAI_BASE_URL": f"http://localhost:{port}/v1",
        "OPENAI_API_KEY": "agentalloy",
    }


def _install_writer(port: int, root: Path, force: bool = False) -> list[WireRecord]:
    """Install wiring for codex by writing ~/.codex/config.toml.

    Creates a TOML config with an apiBaseUrl sentinel-bounded block
    pointing to the AgentAlloy proxy.
    """
    return install.apply_persistent_config(port, root, force)


# Register the harness in the global REGISTRY.
REGISTRY["codex"] = HarnessSpec(
    name="codex",
    binary="codex",
    capabilities=(Capability.PROXY,),
    protocol=Protocol.OPENAI,
    env_builder=_env_builder,
    hook_writer=None,
    install_writer=_install_writer,
)
