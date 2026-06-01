"""Codex runtime module — build_launch_env / env_builder for OpenAI Codex CLI.

Codex is OpenAI's CLI agent that speaks the OpenAI Chat Completions API.
The env_builder configures it to route through the AgentAlloy proxy.
"""

from __future__ import annotations


def build_launch_env(port: int) -> dict[str, str]:
    """Return a minimal env dict for spawning codex via the AgentAlloy proxy.

    Sets OPENAI_BASE_URL and OPENAI_API_KEY so codex uses the proxy endpoint.

    Args:
        port: The AgentAlloy proxy port.

    Returns:
        Environment dict with proxy configuration.
    """
    return {
        "OPENAI_BASE_URL": f"http://localhost:{port}/v1",
        "OPENAI_API_KEY": "agentalloy",
    }
