"""Store factory: open the unified corpus store with the right access mode.

The corpus lives in ONE OverGraph store (skills, versions, fragments,
dependencies, fragment embeddings/HNSW) plus a Tantivy BM25 sidecar for
keyword search. Roles encode the single-writer constraint (the BM25 sidecar
takes an exclusive writer lock):

- ``"service"`` — the serving process opens the store READ-ONLY so a
  reembed / install-pack writer can take the lock without a service stop.
- ``"writer"`` — reembed / install-pack. Read-write (migrates).
- ``"reader"`` — one-shot CLI (doctor / verify / status). Read-only.

Fragments and skills are the SAME store, so ``open_skills`` is the only
opener; ``app`` serves reads off one read-only handle for both surfaces.
"""

from __future__ import annotations

from typing import Literal

from agentalloy.config import Settings, get_settings
from agentalloy.storage.overgraph_skill_store import (
    OverGraphSkillStore,
    open_overgraph_skill_store,
)
from agentalloy.storage.telemetry_store import DuckDBTelemetryStore, open_telemetry_store

Role = Literal["service", "writer", "reader"]


def open_skills(
    settings: Settings | None = None,
    *,
    read_only: bool = False,
) -> OverGraphSkillStore:
    """Open the unified corpus store (skills AND fragment vectors/BM25)."""
    s = settings or get_settings()
    return open_overgraph_skill_store(s.corpus_store_path, read_only=read_only)


def open_telemetry(
    settings: Settings | None = None,
    *,
    read_only: bool = False,
) -> DuckDBTelemetryStore:
    s = settings or get_settings()
    return open_telemetry_store(s.telemetry_db_path, read_only=read_only)
