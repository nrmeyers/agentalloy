"""Claude Code runtime module — build_launch_env / env_builder for Anthropic Claude Code CLI.

Claude Code is Anthropic's CLI agent that speaks the Anthropic Messages API.
The env_builder configures it to route through the AgentAlloy proxy.
"""

from __future__ import annotations

from pathlib import Path


def build_launch_env(port: int) -> dict[str, str]:
    """Return a minimal env dict for spawning claude-code via the AgentAlloy proxy.

    Sets **only** ``ANTHROPIC_BASE_URL`` — never ``ANTHROPIC_API_KEY``: the proxy
    forwards the caller's own credential, so account/OAuth auth keeps working. The
    base URL carries the per-repo ``/proj/<token>`` discriminator (from the cwd) so
    the native passthrough resolves this repo's phase/lifecycle.

    Args:
        port: The AgentAlloy proxy port.

    Returns:
        Environment dict with proxy configuration.
    """
    from agentalloy.api.proxy_context import encode_proj_token

    token = encode_proj_token(Path.cwd())
    # No /v1 suffix: the Anthropic SDK appends /v1/messages to the base URL.
    return {"ANTHROPIC_BASE_URL": f"http://localhost:{port}/proj/{token}"}
