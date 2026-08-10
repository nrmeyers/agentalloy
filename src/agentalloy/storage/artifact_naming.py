"""Canonical naming for store-backed SDD artifacts.

Single source of truth for the suffix a lifecycle artifact carries in the
``sdd_artifact`` name column. After the store-only migration (no artifact is
ever written to disk as a loose file), the suffix is ``.artifact`` — any
``.md`` store name is legacy and is rejected by pack validation
(``_validate_gate_store_names``) and renamed by ``DuckDBStateStore.migrate``.

Importing this module must not import the state store, so it has no internal
deps and can be consumed from both ``graph`` (depends on the store) and
``pack_validation``/``phase.py``.
"""

from __future__ import annotations

#: The extension every store-backed artifact name ends with.
ARTIFACT_EXT = ".artifact"

#: The legacy extension store rows may still carry on upgraded databases.
LEGACY_ARTIFACT_EXT = ".md"

#: Phases whose lifecycle artifacts live in the store, not on disk, and
#: therefore must use ``ARTIFACT_EXT`` for their names.
STORE_BACKED_PHASES = frozenset({"spec", "design", "plan", "qa", "ship", "sdd-fast"})


def is_legacy_artifact_name(name: str) -> bool:
    """Whether ``name`` still carries the deprecated ``.md`` lifecycle suffix."""
    return name.endswith(LEGACY_ARTIFACT_EXT)


def canonicalize_artifact_name(phase: str, name: str) -> str:
    """Return the canonical stored artifact name for ``(phase, name)``.

    Store-backed phases own the ``.artifact`` suffix as an invariant (see the
    module docstring and ``DuckDBStateStore.migrate``). A caller may pass the
    bare logical name (``"delivery"``, ``"qa"``) or a legacy ``.md`` name
    (``"approach.md"``); all of them land in the store with the canonical
    ``.artifact`` suffix so a gate's ``name: "*.artifact"`` glob always matches
    what was written, and legacy ``.md`` rows are repaired on write rather than
    re-broken. Phases outside ``STORE_BACKED_PHASES`` are returned unchanged —
    they are disk deliverables (e.g. ``src/**``) and must not be renamed.
    """
    if phase not in STORE_BACKED_PHASES:
        return name
    stem = name
    if is_legacy_artifact_name(stem):
        stem = stem[: -len(LEGACY_ARTIFACT_EXT)]
    if not stem.endswith(ARTIFACT_EXT):
        stem = stem + ARTIFACT_EXT
    return stem
