"""Backend for the ``graph_query`` tool — subgraph exploration.

* **Non-empty query** — hybrid search (``CodeSearcher``) picks the seed
  symbols; the store expands them ``hops`` deep (both directions) into the
  node/relationship pairs TheForge's graph view consumes.
* **Empty query** — top-PageRank overview: the highest-centrality symbols
  are the seeds, so an unguided call still lands on load-bearing code.

The wire shape is frozen by the HANDOFF contract:
``{nodes: [{id, qname, file, kind, repo, centrality, hopDistance}],
relationships: [{source, target, type, confidence, file}],
row_count, has_more}``.
"""

from __future__ import annotations

from .protocols import CodeGraphStore
from .retrieval.hybrid import CodeSearcher

_SEED_K = 5
"""Hybrid results used as subgraph seeds (more seeds dilute the radius)."""


def graph_query(
    store: CodeGraphStore,
    searcher: CodeSearcher,
    query: str,
    *,
    limit: int = 20,
    hops: int = 1,
    repo: str | None = None,
) -> dict[str, object]:
    """Expand ``query`` (or the top-centrality overview) into a subgraph."""
    query = (query or "").strip()
    if query:
        seeds = [h.qualified_name for h in searcher.search(query, k=_SEED_K)]
    else:
        seeds = [qn for qn, _score in store.top_centrality(limit=_SEED_K)]

    if not seeds:
        return {"nodes": [], "relationships": [], "row_count": 0, "has_more": False}

    nodes, relationships = store.subgraph(seeds, hops=hops, limit=limit, repo=repo)
    return {
        "nodes": [
            {
                "id": n["id"],
                "qname": n["qname"],
                "file": n["file"],
                "kind": n["kind"],
                "repo": n["repo"],
                "centrality": n["centrality"],
                "hopDistance": n["hop_distance"],
            }
            for n in nodes
        ],
        "relationships": [
            {
                "source": r["source"],
                "target": r["target"],
                "type": r["type"],
                "confidence": r["confidence"],
                "file": r["file"],
            }
            for r in relationships
        ],
        "row_count": len(nodes),
        "has_more": len(nodes) >= limit,
    }
