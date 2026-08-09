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
