"""Qwen Code provider — HarnessSpec registration for Qwen Code CLI.

Registers the ``qwen-code`` harness in REGISTRY with:
- Protocol: OPENAI (Qwen Code speaks the OpenAI Chat Completions API)
- Capabilities: PROXY (proxy wiring via settings.json + env vars)
- env_builder: sets OPENAI_BASE_URL + OPENAI_API_KEY pointing at the proxy
- install_writer: writes ~/.qwen/settings.json with proxy model config
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
    """Build environment dict for the qwen-code subprocess.

    Sets OPENAI_BASE_URL and OPENAI_API_KEY so Qwen Code routes through
    the AgentAlloy proxy. Qwen Code natively honors these env vars for its
    upstream LLM connection.
    """
    return {
        "OPENAI_BASE_URL": f"http://localhost:{port}/v1",
        "OPENAI_API_KEY": "agentalloy",
    }


def _install_writer(port: int, root: Path, force: bool = False) -> list[WireRecord]:
    """Install persistent wiring for qwen-code by writing ~/.qwen/settings.json."""
    return install.apply_persistent_config(port, root, force)


# Register the harness in the global REGISTRY.
REGISTRY["qwen-code"] = HarnessSpec(
    name="qwen-code",
    binary="qwen",
    capabilities=(Capability.PROXY,),
    protocol=Protocol.OPENAI,
    env_builder=_env_builder,
    install_writer=_install_writer,
)
