"""Qwen Code install module — apply_persistent_config / install_writer.

Proxy wiring: repo-local ``.qwen/settings.json`` (QWEN_HOME isolation) with
the ``model`` block pointed at the proxy's ``/proj/<token>`` endpoint, plus
activation carriers and a ``.qwen/.agentalloy-env`` file.

Delegates to ``wire_harness._wire_proxy_qwen_code`` so the provider registry
and ``agentalloy wire`` share one code path.
"""

from __future__ import annotations

from pathlib import Path

from agentalloy.providers.base import WireRecord


def apply_persistent_config(port: int, root: Path, force: bool = False) -> list[WireRecord]:
    """Install persistent proxy wiring for qwen-code.

    Delegates to the live ``_wire_proxy_qwen_code`` implementation (repo-local
    ``.qwen/settings.json`` + ``QWEN_HOME`` activation carriers) so this
    provider module and ``agentalloy wire`` share one code path.

    Args:
        port: The AgentAlloy proxy port.
        root: The repository root.
        force: Unused — wiring replaces only files it owns.

    Returns:
        List of WireRecord describing files written.
    """
    # Lazy import: wire_harness imports the provider registry at module load,
    # so a top-level import here would be circular.
    from agentalloy.install.subcommands.wire_harness import (  # pyright: ignore[reportPrivateUsage]
        _wire_proxy_qwen_code,
    )

    _ = force
    records = _wire_proxy_qwen_code(port, root, scope="repo")
    return [WireRecord.from_dict(r) for r in records]
