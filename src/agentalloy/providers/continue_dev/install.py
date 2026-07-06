"""Continue.dev install module — apply_persistent_config / install_writer.

Proxy wiring: a ``models[]`` entry in ``.continuerc.json`` pointing at the
proxy (``apiBase``), plus the ``_agentalloy_install_marker`` for clean
removal. The implementation lives in ``wire_harness._wire_proxy_continue``
(shared with ``agentalloy wire``); this module delegates so the provider
registry and the live wiring can never diverge.
"""

from __future__ import annotations

from pathlib import Path

from agentalloy.providers.base import WireRecord


def apply_persistent_config(
    port: int, root: Path, force: bool = False, harness: str = "continue-closed"
) -> list[WireRecord]:
    """Install persistent proxy wiring for Continue.dev.

    Delegates to the live ``_wire_proxy_continue`` implementation so this
    provider module and ``agentalloy wire`` share one code path.

    Args:
        port: The AgentAlloy proxy port.
        root: The repository root.
        force: Unused — the proxy model entry is idempotently replaced.
        harness: ``"continue-closed"`` or ``"continue-local"`` (recorded in the
            install marker's ``variant`` field).

    Returns:
        List of WireRecord describing files written.
    """
    # Lazy import: wire_harness imports the provider registry at module load,
    # so a top-level import here would be circular.
    from agentalloy.install.subcommands.wire_harness import (
        _wire_proxy_continue,  # pyright: ignore[reportPrivateUsage]
    )

    _ = force
    records = _wire_proxy_continue(harness, port, root)
    return [WireRecord.from_dict(r) for r in records]
