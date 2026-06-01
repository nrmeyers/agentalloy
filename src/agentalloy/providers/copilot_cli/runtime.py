"""Copilot CLI runtime environment builder."""

from __future__ import annotations


def build_launch_env(port: int) -> dict[str, str]:
    """Build environment for spawning copilot CLI.

    GitHub Copilot CLI is a sidecar harness that uses markdown injection,
    not proxy. Returns an empty env dict.

    Args:
        port: The AgentAlloy proxy port (unused for this harness).

    Returns:
        Empty environment dict.
    """
    return {}
