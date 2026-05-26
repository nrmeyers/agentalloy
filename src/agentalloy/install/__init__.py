"""Install CLI — idempotent subcommands for the INSTALL.md runbook.

Usage::

    python -m agentalloy.install <subcommand> [args]

Subcommands are organised under ``agentalloy.install.subcommands``.
Each module exposes ``add_parser(subparsers)`` and ``run(args) -> int``.
"""

# Harnesses whose LLM traffic cannot be intercepted by the AgentAlloy proxy
# (no first-party base-URL override, or routes through their own backend).
# They require legacy markdown-injection wiring or the sidecar file watcher.
PROXY_UNABLE_HARNESSES: frozenset[str] = frozenset(
    {"cursor", "windsurf", "github-copilot", "gemini-cli"}
)
