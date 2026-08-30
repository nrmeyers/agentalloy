"""Storage adapters — unified OverGraph corpus store.

- OverGraph ``agentalloy.overgraph``: skills, versions, fragments,
  dependencies, and fragment embeddings (HNSW) in one embedded graph DB,
  plus a Tantivy BM25 sidecar for keyword search.
  -> ``overgraph_skill_store.OverGraphSkillStore``
- DuckDB ``telemetry.duck``: composition traces.  -> ``telemetry_store.DuckDBTelemetryStore``
- DuckDB ``state.duck``: SDD lifecycle runtime state (phase, cursor,
  cadence markers).  -> ``state_store.DuckDBStateStore``

Shared DTOs / constants / Protocols live in ``protocols``. Use the
per-engine openers (``open_skills`` / ``open_telemetry``) to construct
handles.
"""

from __future__ import annotations

from agentalloy.storage.open import (
    open_skills,
    open_telemetry,
)
from agentalloy.storage.overgraph_skill_store import (
    OverGraphSkillStore,
    open_overgraph_skill_store,
)
from agentalloy.storage.protocols import (
    EMBEDDING_DIM,
    BM25Hit,
    CompositionTrace,
    EmbeddingDimMismatch,
    FragmentEmbedding,
    FragmentStore,
    LeaseConflict,
    LeaseResult,
    LockHeldError,
    SimilarityHit,
    SkillStore,
    StateStore,
    StateWriteResult,
    TelemetryStore,
    VectorStoreError,
    is_lock_held_error,
    l2_normalize,
)
from agentalloy.storage.state_store import (
    DuckDBStateStore,
    StateStoreError,
    open_state_store,
)
from agentalloy.storage.telemetry_store import DuckDBTelemetryStore, open_telemetry_store

__all__ = [
    # constants / errors / helpers
    "EMBEDDING_DIM",
    "EmbeddingDimMismatch",
    "VectorStoreError",
    "LockHeldError",
    "is_lock_held_error",
    "StateStoreError",
    "l2_normalize",
    # DTOs
    "FragmentEmbedding",
    "SimilarityHit",
    "BM25Hit",
    "CompositionTrace",
    "StateWriteResult",
    "LeaseResult",
    "LeaseConflict",
    # protocols
    "FragmentStore",
    "SkillStore",
    "TelemetryStore",
    "StateStore",
    # concrete stores
    "OverGraphSkillStore",
    "DuckDBTelemetryStore",
    "DuckDBStateStore",
    # factories
    "open_skills",
    "open_telemetry",
    "open_overgraph_skill_store",
    "open_telemetry_store",
    "open_state_store",
]
