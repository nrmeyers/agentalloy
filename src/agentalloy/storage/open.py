"""Store factory: open the three v5 engines with the right access mode per role.

Roles encode the DuckDB cross-process locking constraint (decision D4):

- ``"service"`` — the serving process. ``agentalloy.duck`` is opened READ-ONLY
  and transiently: the caller loads the in-memory RuntimeCache then closes the
  skill handle so an ingest/reembed writer can take the exclusive lock without a
  service stop. ``telemetry.duck`` is opened read-write and held for the
  process lifetime (service-owned). Lance has no exclusive lock.
- ``"writer"`` — ingest / reembed. ``agentalloy.duck`` read-write (migrates).
- ``"reader"`` — one-shot CLI (doctor / verify / status). Everything read-only.

Each engine also has a single-store opener for callers that need only one
(e.g. ``app`` reopening skills to reload the cache after a reembed).
"""

from __future__ import annotations

from typing import Literal

from agentalloy.config import Settings, get_settings
from agentalloy.storage.fragment_store import LanceFragmentStore
from agentalloy.storage.protocols import EMBEDDING_DIM, EmbeddingDimMismatch, Stores
from agentalloy.storage.skill_store import DuckDBSkillStore, open_skill_store
from agentalloy.storage.telemetry_store import DuckDBTelemetryStore, open_telemetry_store

Role = Literal["service", "writer", "reader"]


def open_fragments(settings: Settings | None = None) -> LanceFragmentStore:
    s = settings or get_settings()
    store = LanceFragmentStore(s.fragments_lance_path)
    dim = store.embedding_dim()
    if dim is not None and dim != EMBEDDING_DIM:
        # Largely unreachable (Lance FixedSizeList is dim-fixed) but kept for the
        # multi-surface dim contract; message carries an upgrade.py marker substring.
        raise EmbeddingDimMismatch(
            f"fragments dataset has {dim}-dim embeddings but runtime expects "
            f"{EMBEDDING_DIM}-dim (nomic-embed-text-v1.5)"
        )
    return store


def open_skills(settings: Settings | None = None, *, read_only: bool = False) -> DuckDBSkillStore:
    s = settings or get_settings()
    return open_skill_store(s.duckdb_path, read_only=read_only)


def open_telemetry(
    settings: Settings | None = None, *, read_only: bool = False
) -> DuckDBTelemetryStore:
    s = settings or get_settings()
    return open_telemetry_store(s.telemetry_db_path, read_only=read_only)


def open_stores(settings: Settings | None = None, *, role: Role = "service") -> Stores:
    """Open all three stores with access modes appropriate to ``role``."""
    s = settings or get_settings()
    if role == "writer":
        s.ensure_data_dirs()
        skills_ro, tel_ro = False, False
    elif role == "reader":
        skills_ro, tel_ro = True, True
    else:  # service
        skills_ro, tel_ro = True, False
    return Stores(
        fragments=open_fragments(s),
        skills=open_skills(s, read_only=skills_ro),
        telemetry=open_telemetry(s, read_only=tel_ro),
    )
