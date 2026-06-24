"""Openclaw provider — HarnessSpec registration for Openclaw plugin harness.

Registers the ``openclaw`` harness in REGISTRY with:
- Protocol: OPENAI (Openclaw speaks the OpenAI Chat Completions API)
- Capabilities: PROXY (proxy wiring via env vars + persistent plugin config)
- env_builder: sets OPENAI_BASE_URL for the openclaw binary
- install_writer: writes ~/.openclaw/plugins.json with agentalloy plugin entry
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

    Sets OPENAI_BASE_URL so openclaw routes API calls through the
    AgentAlloy proxy. The base URL carries the per-repo ``/proj/<token>``
    discriminator (realpath of cwd) so the proxy resolves this repo's
    phase/lifecycle — parity with the Anthropic path.
    """
    from pathlib import Path

    from agentalloy.api.proxy_context import encode_proj_token

    token = encode_proj_token(Path.cwd())
    return {
        "OPENAI_BASE_URL": f"http://localhost:{port}/proj/{token}/v1",
        "OPENAI_API_KEY": "agentalloy",
    }


def _install_writer(port: int, root: Path, force: bool = False) -> list[WireRecord]:
    """Install wiring for openclaw by writing ~/.openclaw/plugins.json.

    Creates a JSON plugin config with an agentalloy plugin entry
    pointing to the AgentAlloy proxy.
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
