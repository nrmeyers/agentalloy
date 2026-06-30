"""Storage adapters — v5 two-engine backend.

- LanceDB ``fragments`` dataset: vector ANN (retrieval) + exact cosine (dedup)
  + native BM25.  -> ``fragment_store.LanceFragmentStore``
- DuckDB ``agentalloy.duck``: skill metadata (folded out of the legacy graph engine) + corpus_meta.
  -> ``skill_store.DuckDBSkillStore``
- DuckDB ``telemetry.duck``: composition traces.  -> ``telemetry_store.DuckDBTelemetryStore``

Shared DTOs / constants / Protocols live in ``protocols``. Use ``open_stores``
(or the per-engine openers) to construct handles.
"""

from __future__ import annotations

from agentalloy.storage.fragment_store import FRAGMENTS_SCHEMA, LanceFragmentStore
from agentalloy.storage.open import (
    open_fragments,
    open_skills,
    open_stores,
    open_telemetry,
)
from agentalloy.storage.protocols import (
    EMBEDDING_DIM,
    BM25Hit,
    CompositionTrace,
    EmbeddingDimMismatch,
    FragmentEmbedding,
    FragmentStore,
    SimilarityHit,
    SkillStore,
    Stores,
    TelemetryStore,
    VectorStoreError,
    l2_normalize,
)
from agentalloy.storage.skill_store import (
    DuckDBSkillStore,
    LockHeldError,
    is_lock_held_error,
    open_skill_store,
)
from agentalloy.storage.telemetry_store import DuckDBTelemetryStore, open_telemetry_store

__all__ = [
    # constants / errors / helpers
    "EMBEDDING_DIM",
    "EmbeddingDimMismatch",
    "VectorStoreError",
    "LockHeldError",
    "is_lock_held_error",
    "l2_normalize",
    # DTOs
    "FragmentEmbedding",
    "SimilarityHit",
    "BM25Hit",
    "CompositionTrace",
    # protocols + bundle
    "FragmentStore",
    "SkillStore",
    "TelemetryStore",
    "Stores",
    # concrete stores
    "LanceFragmentStore",
    "FRAGMENTS_SCHEMA",
    "DuckDBSkillStore",
    "DuckDBTelemetryStore",
    # factories
    "open_stores",
    "open_fragments",
    "open_skills",
    "open_telemetry",
    "open_skill_store",
    "open_telemetry_store",
]
