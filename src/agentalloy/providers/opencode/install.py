"""OpenCode install module — apply_persistent_config / install_writer.

Proxy wiring: repo-local ``opencode.json`` with an ``agentalloy`` provider on
``@ai-sdk/openai-compatible`` (Chat Completions wire) pointed at the proxy's
per-repo ``/proj/<token>/v1`` endpoint, selected as the default model. The
implementation lives in ``wire_harness._wire_proxy_opencode`` (shared with
``agentalloy wire``); this module delegates so the provider registry and the
live wiring can never diverge.
"""

from __future__ import annotations

from pathlib import Path

from agentalloy.providers.base import WireRecord


def apply_persistent_config(port: int, root: Path, force: bool = False) -> list[WireRecord]:
    """Install persistent proxy wiring for OpenCode.

    Delegates to the live ``_wire_proxy_opencode`` implementation so this
    provider module and ``agentalloy wire`` share one code path.

    Args:
        port: The AgentAlloy proxy port.
        root: The repository root.
        force: Unused — the env file and sentinel block are idempotently replaced.

    Returns:
        List of WireRecord describing files written.
    """
    # Lazy import: wire_harness imports the provider registry at module load,
    # so a top-level import here would be circular.
    from agentalloy.install.subcommands.wire_harness import (
        _wire_proxy_opencode,  # pyright: ignore[reportPrivateUsage]
    )

    _ = force
    records = _wire_proxy_opencode(port, root)
    return [WireRecord.from_dict(r) for r in records]
