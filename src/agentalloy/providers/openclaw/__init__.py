"""Openclaw provider — HarnessSpec registration for Openclaw plugin harness.

Registers the ``openclaw`` harness in REGISTRY with:
- Protocol: OPENAI (Openclaw speaks the OpenAI Chat Completions API)
- Capabilities: PROXY (user-scoped custom model provider in openclaw.json)
- env_builder: returns empty dict (openclaw does not honor OPENAI_BASE_URL;
  the config file is the only working vector — e2e-matrix finding)
- install_writer: merges models.providers.agentalloy into ~/.openclaw/openclaw.json
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
    """Build environment dict for the openclaw subprocess.

    OpenClaw does not honor OPENAI_BASE_URL (verified against a live binary);
    routing comes entirely from the ~/.openclaw/openclaw.json custom provider
    the install_writer merges. Returns an empty dict.
    """
    _ = port
    return {}


def _install_writer(port: int, root: Path, force: bool = False) -> list[WireRecord]:
    """Install wiring for openclaw via ~/.openclaw/openclaw.json.

    Merges the agentalloy custom model provider + default model into the
    user-scoped config (bare /v1 surface — openclaw is not repo-scoped).
    """
    return install.apply_persistent_config(port, root, force)


# Register the harness in the global REGISTRY.
REGISTRY["openclaw"] = HarnessSpec(
    name="openclaw",
    binary="openclaw",
    capabilities=(Capability.PROXY,),
    protocol=Protocol.OPENAI,
    env_builder=_env_builder,
    install_writer=_install_writer,
)
