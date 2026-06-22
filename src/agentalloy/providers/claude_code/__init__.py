"""Claude Code provider — HarnessSpec registration for Anthropic Claude Code CLI.

Registers the ``claude-code`` harness in REGISTRY with:
- Protocol: ANTHROPIC (Claude Code speaks the Anthropic Messages API)
- Capabilities: PROXY
- env_builder: sets ONLY ANTHROPIC_BASE_URL (auth-transparent) for the claude binary
- install_writer: writes the proxy carrier env file (ANTHROPIC_BASE_URL only)

claude-code wires through the native proxy: env vars point the Anthropic SDK at
the AgentAlloy proxy, which injects composed skills and forwards upstream.
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

    Sets **only** ``ANTHROPIC_BASE_URL`` — never ``ANTHROPIC_API_KEY``: the proxy
    forwards the caller's own credential verbatim, so account/OAuth auth (Pro/Max/
    Team users with no API key) keeps working. The base URL carries the per-repo
    ``/proj/<token>`` discriminator (encoded from the cwd the child runs in) so the
    native Anthropic passthrough resolves this repo's phase/lifecycle.
    """
    from agentalloy.api.proxy_context import encode_proj_token

    token = encode_proj_token(Path.cwd())
    # No /v1 suffix: the Anthropic SDK appends /v1/messages to the base URL,
    # so a /v1 here produces /v1/v1/messages (404 against the proxy).
    return {"ANTHROPIC_BASE_URL": f"http://localhost:{port}/proj/{token}"}


def _install_writer(port: int, root: Path, force: bool = False) -> list[WireRecord]:
    """Install wiring for claude-code by writing ~/.agentalloy/claude-code-env.sh.

    Creates a shell script with sentinel-bounded environment variable exports
    pointing to the AgentAlloy proxy.
    """
    return install.apply_persistent_config(port, root, force)


# Register the harness in the global REGISTRY. claude-code wires through the
# native proxy (see module docstring).
REGISTRY["claude-code"] = HarnessSpec(
    name="claude-code",
    binary="claude",
    capabilities=(Capability.PROXY,),
    protocol=Protocol.ANTHROPIC,
    env_builder=_env_builder,
    install_writer=_install_writer,
)
