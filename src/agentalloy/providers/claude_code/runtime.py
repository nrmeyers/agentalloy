"""Claude Code runtime module — build_launch_env / env_builder for Anthropic Claude Code CLI.

Claude Code is Anthropic's CLI agent that speaks the Anthropic Messages API.
The env_builder configures it to route through the AgentAlloy proxy.
"""

from __future__ import annotations


def build_launch_env(port: int) -> dict[str, str]:
    """Return a minimal env dict for spawning claude-code via the AgentAlloy proxy.

    Sets ANTHROPIC_BASE_URL and ANTHROPIC_API_KEY so claude-code uses
    the proxy endpoint.

    Args:
        port: The AgentAlloy proxy port.

    Returns:
        Environment dict with proxy configuration.
    """
    return {
        # No /v1 suffix: the Anthropic SDK appends /v1/messages to the base URL.
        "ANTHROPIC_BASE_URL": f"http://localhost:{port}",
        "ANTHROPIC_API_KEY": "agentalloy",
    }
