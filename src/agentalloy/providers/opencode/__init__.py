"""OpenCode provider — HarnessSpec registration for OpenCode CLI.

Registers the ``opencode`` harness in REGISTRY with:
- Protocol: OPENAI (OpenCode speaks the OpenAI Chat Completions API)
- Capabilities: PROXY (proxy wiring via env file + system prompt)
- env_builder: sets OPENAI_API_BASE and OPENAI_API_KEY
- install_writer: writes .opencode/.agentalloy-env + .opencode/system-prompt.md
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
    through the AgentAlloy proxy. The base URL carries the per-repo
    ``/proj/<token>`` discriminator (realpath of cwd) so the proxy resolves this
    repo's phase/lifecycle — parity with the Anthropic path.
    """
    from pathlib import Path

    from agentalloy.api.proxy_context import encode_proj_token

    token = encode_proj_token(Path.cwd())
    return {
        "OPENAI_API_BASE": f"http://localhost:{port}/proj/{token}/v1",
        "OPENAI_API_KEY": "agentalloy",
    }


def _install_writer(port: int, root: Path, force: bool = False) -> list[WireRecord]:
    """Install persistent wiring for opencode.

    Writes .opencode/.agentalloy-env and .opencode/system-prompt.md.
    """
    return install.apply_persistent_config(port, root, force)


# Register the harness in the global REGISTRY.
REGISTRY["opencode"] = HarnessSpec(
    name="opencode",
    binary="opencode",
    capabilities=(Capability.PROXY,),
    protocol=Protocol.OPENAI,
    env_builder=_env_builder,
    install_writer=_install_writer,
)
