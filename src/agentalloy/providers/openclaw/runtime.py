"""Openclaw runtime module — env_builder for Openclaw plugin harness.

The env_builder configures the environment so openclaw routes API calls
through the AgentAlloy proxy.
"""

from __future__ import annotations


def build_launch_env(port: int) -> dict[str, str]:
    """Return a minimal env dict for spawning openclaw via the AgentAlloy proxy.

    Sets OPENAI_BASE_URL and OPENAI_API_KEY so openclaw uses the proxy endpoint.

    Args:
        port: The AgentAlloy proxy port.

    Returns:
        Environment dict with proxy configuration.
    """
    return {
        "OPENAI_BASE_URL": f"http://localhost:{port}/v1",
        "OPENAI_API_KEY": "agentalloy",
    }


def env_builder(port: int) -> dict[str, str]:
    """Build environment dict for the openclaw subprocess.

    Sets OPENAI_BASE_URL so openclaw routes API calls through the
    AgentAlloy proxy.

    Args:
        port: The AgentAlloy proxy port.

    Returns:
        Environment dict with proxy configuration.
    """
    return build_launch_env(port)
