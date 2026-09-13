"""Re-embed CLI: walks the skill store's fragments, embeds via the embed
server, and writes L2-normalized rows to the corpus store's fragment
embedding index.

Idempotent — skips fragments whose fragment_id is already embedded.
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
