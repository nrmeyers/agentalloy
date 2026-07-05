"""PageRank centrality over the CALLS graph — pure-Python power iteration.

Ports the algorithm contract from codebase-indexer's ``app/services/pagerank.py``
(alpha=0.85, max_iter=100, hard node ceiling) WITHOUT networkx/scipy: the code
graphs we rank are small enough (ceiling 100k nodes) that a dict-and-list
power iteration converges in milliseconds, and it drops two heavyweight
dependencies from the wheel.

Semantics match ``networkx.pagerank`` defaults: damping alpha, uniform
teleport, dangling-node mass redistributed uniformly, parallel edges
deduplicated, convergence on L1 error < N * tol.
"""

from __future__ import annotations

import logging

from agentalloy.storage.protocols import CodeGraphStore

logger = logging.getLogger(__name__)

_ALPHA = 0.85
_MAX_ITER = 100
_TOL = 1.0e-6
# Bounded compute — protect the indexing hot path from pathological graphs.
_NODE_CEILING = 100_000


def compute_pagerank(edges: list[tuple[str, str]]) -> dict[str, float]:
    """Raw PageRank over an in-memory CALLS graph. Pure: no I/O, no DB.

    Nodes are inferred from the edge endpoints; duplicate edges are collapsed
    (matching networkx ``DiGraph`` semantics). Returns scores that sum to
    ~1.0. Returns ``{}`` for an empty graph or one exceeding the node ceiling
    (logged at WARN).
    """
    edge_set = {(s, d) for s, d in edges if isinstance(s, str) and isinstance(d, str)}
    if not edge_set:
        return {}

    nodes = sorted({n for e in edge_set for n in e})
    n = len(nodes)
    if n > _NODE_CEILING:
        logger.warning("pagerank.skipped reason=node_ceiling nodes=%d ceiling=%d", n, _NODE_CEILING)
        return {}

    idx = {qn: i for i, qn in enumerate(nodes)}
    out: list[list[int]] = [[] for _ in range(n)]
    for src, dst in edge_set:
        out[idx[src]].append(idx[dst])

    teleport = (1.0 - _ALPHA) / n
    rank = [1.0 / n] * n
    for _ in range(_MAX_ITER):
        dangling_mass = sum(rank[i] for i in range(n) if not out[i])
        base = teleport + _ALPHA * dangling_mass / n
        nxt = [base] * n
        for i, targets in enumerate(out):
            if targets:
                share = _ALPHA * rank[i] / len(targets)
                for j in targets:
                    nxt[j] += share
        err = sum(abs(nxt[i] - rank[i]) for i in range(n))
        rank = nxt
        if err < n * _TOL:
            break

    return {nodes[i]: rank[i] for i in range(n)}


def refresh_centrality(graph: CodeGraphStore) -> int:
    """Recompute PageRank from the graph's CALLS edges and persist it.

    Replaces the ``centrality`` snapshot wholesale via ``write_centrality``
    (an empty/over-ceiling graph clears it). Returns rows written.
    """
    scores = compute_pagerank(graph.calls_edges())
    return graph.write_centrality(scores)
