"""Manual provider — HarnessSpec registration for the manual harness.

Registers the ``manual`` harness in REGISTRY with:
- Protocol: EITHER (manual harness works with any protocol)
- Capabilities: MARKDOWN_ONLY (prints instruction block to stdout)
- env_builder: returns empty dict (manual harness doesn't spawn processes)
- install_writer: None (manual harness prints to stdout, no file writes)
"""

from __future__ import annotations

from agentalloy.providers import REGISTRY
from agentalloy.providers.base import (
    Capability,
    HarnessSpec,
    Protocol,
)


def _env_builder(port: int) -> dict[str, str]:
    """Build environment dict for the manual harness.

    The manual harness doesn't spawn a subprocess — it prints instructions.
    Returns an empty dict.
    """
    return {}


# Register the harness in the global REGISTRY.
REGISTRY["manual"] = HarnessSpec(
    name="manual",
    binary="manual",
    capabilities=(Capability.MARKDOWN_ONLY,),
    protocol=Protocol.EITHER,
    env_builder=_env_builder,
    install_writer=None,
)
