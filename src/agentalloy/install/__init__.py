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
    # "gemini-cli" is the deprecated alias for antigravity (Antigravity CLI).
    {"cursor", "windsurf", "github-copilot", "antigravity", "gemini-cli"}
)

# Harnesses that use the native Anthropic passthrough (`/proj/<token>/v1/messages`)
# rather than the OpenAI-compatible bridge. They forward the caller's own
# credential to the Anthropic upstream (`ANTHROPIC_UPSTREAM_URL`, default
# api.anthropic.com), so the setup wizard must NOT prompt them for the OpenAI
# `UPSTREAM_URL` — they don't use it.
NATIVE_PASSTHROUGH_HARNESSES: frozenset[str] = frozenset({"claude-code"})
