"""Re-embed CLI: walks the skill store's fragments, embeds via LM Studio,
writes L2-normalized rows to DuckDB fragment_embeddings.

Idempotent — skips fragments whose fragment_id is already in DuckDB.
Bounded retries on transient embedding-call failures.

Entry point: ``python -m agentalloy.reembed``
"""

from agentalloy.reembed.cli import (
    FragmentNeedingEmbedding,
    ReembedStats,
    discover_unembedded_fragments,
    reembed_fragments,
)

__all__ = [
    "FragmentNeedingEmbedding",
    "ReembedStats",
    "discover_unembedded_fragments",
    "reembed_fragments",
]
