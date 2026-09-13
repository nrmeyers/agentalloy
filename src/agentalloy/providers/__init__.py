"""Provider registry package.

Exports:
    REGISTRY: dict[str, HarnessSpec] — the central lookup table for all
        registered harnesses.  Keys are lowercase strings (e.g. ``"claude-code"``).
    HarnessSpec, Capability, Protocol, WireRecord — from ``base.py``.

Providers are auto-discovered and registered at import time by scanning the
``providers`` package directory for subpackages.  Each provider's ``__init__.py``
populates ``REGISTRY`` with its ``HarnessSpec``.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from pathlib import Path

from agentalloy.providers.base import (
    Capability,
    HarnessSpec,
    Protocol,
    WireRecord,
)

logger = logging.getLogger(__name__)

REGISTRY: dict[str, HarnessSpec] = {}

# Auto-discover and import all provider subpackages so they register themselves.
# This happens once at import time, not repeatedly.
_PROVIDERS_DIR = Path(__file__).resolve().parent
for _mod_info in pkgutil.iter_modules([str(_PROVIDERS_DIR)]):
    # Skip non-package modules (e.g. base.py) and the package itself.
    if _mod_info.name in ("base",):
        continue
    try:
        importlib.import_module(f".{_mod_info.name}", package=__name__)
    except Exception as _exc:  # noqa: BLE001 — keep discovering other providers
        # Surface the failure instead of swallowing it: a provider that fails to
        # import is silently absent from REGISTRY and only shows up later as an
        # opaque "unknown harness" at wire time.
        logger.warning("provider %r failed to load: %s", _mod_info.name, _exc)

__all__ = [
    "Capability",
    "HarnessSpec",
    "Protocol",
    "REGISTRY",
    "WireRecord",
]
