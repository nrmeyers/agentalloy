"""Claude Code provider — HarnessSpec registration for Anthropic Claude Code CLI.

Registers the ``claude-code`` harness in REGISTRY with:
- Protocol: ANTHROPIC (Claude Code speaks the Anthropic Messages API)
- Capabilities: PROXY (proxy wiring via env vars)
- env_builder: sets ANTHROPIC_BASE_URL and ANTHROPIC_API_KEY for the claude binary
- install_writer: writes ~/.agentalloy/claude-code-env.sh with proxy config
- hook_writer: None (Claude Code does not use Claude Code hooks)
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
    """Build environment dict for the claude-code subprocess.

    Sets ANTHROPIC_BASE_URL and ANTHROPIC_API_KEY so claude-code routes
    API calls through the AgentAlloy proxy.
    """
    return {
        # No /v1 suffix: the Anthropic SDK appends /v1/messages to the base URL,
        # so a /v1 here produces /v1/v1/messages (404 against the proxy).
        "ANTHROPIC_BASE_URL": f"http://localhost:{port}",
        "ANTHROPIC_API_KEY": "agentalloy",
    }


def _install_writer(port: int, root: Path, force: bool = False) -> list[WireRecord]:
    """Install wiring for claude-code by writing ~/.agentalloy/claude-code-env.sh.

    Creates a shell script with sentinel-bounded environment variable exports
    pointing to the AgentAlloy proxy.
    """
    return install.apply_persistent_config(port, root, force)


def _hook_writer(port: int, root: Path) -> list[WireRecord]:
    """Hook writer for claude-code — not applicable for proxy wiring."""
    return []


# Register the harness in the global REGISTRY.
REGISTRY["claude-code"] = HarnessSpec(
    name="claude-code",
    binary="claude",
    capabilities=(Capability.PROXY,),
    protocol=Protocol.ANTHROPIC,
    env_builder=_env_builder,
    hook_writer=_hook_writer,
    install_writer=_install_writer,
)
