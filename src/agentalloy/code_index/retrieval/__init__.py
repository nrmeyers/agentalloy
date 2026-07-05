"""Read-side retrieval for the code-index module.

- ``hybrid``  — dense + BM25 hybrid search with PageRank fusion and RRF.
- ``bundle``  — task-seeded context bundles (search + call-graph expansion +
  budgeted source assembly).
"""

from __future__ import annotations

from agentalloy.code_index.retrieval.bundle import Bundle, BundleItem, build_bundle
from agentalloy.code_index.retrieval.hybrid import SearchResult, lexical_search, semantic_search

__all__ = [
    "Bundle",
    "BundleItem",
    "SearchResult",
    "build_bundle",
    "lexical_search",
    "semantic_search",
]
