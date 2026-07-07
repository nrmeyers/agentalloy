"""Cline provider — HarnessSpec registration for Cline VS Code extension.

Registers the ``cline`` harness in REGISTRY with:
- Protocol: OPENAI (Cline speaks the OpenAI Chat Completions API)
- Capabilities: PROXY (proxy wiring via the user-scoped providers.json store)
- env_builder: returns empty dict (Cline reads its user-scoped provider store)
- install_writer: merges ~/.cline/data/settings/providers.json with proxy API fields
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

    Cline reads its user-scoped providers.json store rather than env vars.
    Returns an empty dict.
    """
    return {}


def _install_writer(port: int, root: Path, force: bool = False) -> list[WireRecord]:
    """Install persistent wiring for cline.

    Merges the openai-compatible provider entry into
    ~/.cline/data/settings/providers.json (the store cline auth writes).
    """
    return install.apply_persistent_config(port, root, force)


# Register the harness in the global REGISTRY.
REGISTRY["cline"] = HarnessSpec(
    name="cline",
    binary="cline",
    capabilities=(Capability.PROXY,),
    protocol=Protocol.OPENAI,
    env_builder=_env_builder,
    install_writer=_install_writer,
)
