"""Storage for the code index.

One shared OverGraph database (``graph.overgraph``) for all registered
repos — symbols, edges, centrality, decision docs, dense vectors. The
v1 job store / per-repo open helpers are intentionally NOT vendored:
v2 ingest is synchronous per the frozen ``POST /reindex`` contract.
"""

from __future__ import annotations

from agentalloy.code_index.store.overgraph_store import OverGraphCodeGraphStore
from agentalloy.code_index.store.pagerank import compute_pagerank, refresh_centrality

__all__ = [
    "OverGraphCodeGraphStore",
    "compute_pagerank",
    "refresh_centrality",
]
