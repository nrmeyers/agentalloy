"""Stub install module — apply_persistent_config / install_writer placeholder."""

from pathlib import Path

from agentalloy.providers.base import WireRecord


def apply_persistent_config(port: int, root: Path, force: bool = False) -> list[WireRecord]:
    """Install wiring for this harness.  Stub: returns empty list."""
    return []
