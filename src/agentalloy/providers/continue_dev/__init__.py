"""Continue.dev provider — HarnessSpec registration for Continue.dev IDE extension.

Registers the ``continue-closed`` and ``continue-local`` harnesses in REGISTRY with:
- Protocol: OPENAI (Continue speaks the OpenAI Chat Completions API)
- Capabilities: PROXY (proxy wiring via .continuerc.json)
- env_builder: returns empty dict (Continue uses file-based config)
- install_writer: writes .continuerc.json with proxy model config
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
    """Build environment dict for the continue-dev subprocess.

    Continue.dev uses file-based config (.continuerc.json) rather than env vars.
    Returns an empty dict.
    """
    return {}


def _install_writer(port: int, root: Path, force: bool = False) -> list[WireRecord]:
    """Install persistent wiring for continue-dev.

    Writes .continuerc.json with proxy model configuration.
    """
    return install.apply_persistent_config(port, root, force)


# Register the ``continue-closed`` harness in the global REGISTRY.
REGISTRY["continue-closed"] = HarnessSpec(
    name="continue-closed",
    binary="continue",
    capabilities=(Capability.PROXY,),
    protocol=Protocol.OPENAI,
    env_builder=_env_builder,
    install_writer=_install_writer,
)

# Register the ``continue-local`` harness in the global REGISTRY.
REGISTRY["continue-local"] = HarnessSpec(
    name="continue-local",
    binary="continue",
    capabilities=(Capability.PROXY,),
    protocol=Protocol.OPENAI,
    env_builder=_env_builder,
    install_writer=_install_writer,
)
