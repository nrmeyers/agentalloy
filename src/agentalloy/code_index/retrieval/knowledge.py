"""Knowledge retrieval: the three knowledge tool surfaces over the graph.

These back the ``knowledge_why`` / ``knowledge_related`` /
``knowledge_entities`` executors. They read only — no writes — so they are
safe from any request handler.

* :func:`governing_decision` — the single best decision governing a symbol
  (exact-FQN links rank above unique-short-name links).
* :func:`related_decisions` — decisions relevant to a free-text query, found
  two ways: decision chunks that match the query directly, and decisions that
  govern code symbols the query matches (indirect credit, decayed).
* :func:`entity_edges` — the typed entity edges (Requires / Touches /
  Constraints / Govern) around a symbol or chunk, in both directions, with
  the far endpoint hydrated (a target that resolved to nothing is reported
  as dangling, not dropped).
"""

from __future__ import annotations

from typing import Any

from ..protocols import CodeGraphStore
from .hybrid import CodeSearcher

_SNIPPET_MAX_CHARS = 200
_EXCERPT_CAP = 200
# A decision that governs a code hit the query matched earns this fraction of
# the code hit's score — indirect evidence, ranked below a direct chunk hit.
_GOVERNS_CREDIT = 0.5
# Max rows returned by :func:`entity_edges` (declared edges per doc are small;
# this guards a pathologically connected chunk).
_ENTITY_LIMIT = 50


def _excerpt(text: str | None) -> str:
    """First ~200 chars of a chunk body / source, whitespace-trimmed."""
    return (text or "").strip()[:_EXCERPT_CAP]


def _decision_row(
    store: CodeGraphStore,
    qn: str,
    *,
    span: str | None = None,
    tier: int | None = None,
) -> dict[str, Any] | None:
    """Hydrate a decision chunk qn into a row; ``None`` if the chunk row is
    gone (dangling governance edge)."""
    sym = store.symbol(qn)
    if sym is None:
        return None
    row: dict[str, Any] = {
        "qualified_name": sym.qualified_name,
        "heading": sym.name,
        "file_path": sym.file_path,
        "start_line": sym.start_line,
        "excerpt": _excerpt(sym.source_code),
    }
    if span is not None:
        row["span"] = span
    if tier is not None:
        row["resolution_tier"] = tier
    return row


def governing_decision(store: CodeGraphStore, fqn: str) -> dict[str, Any] | None:
    """The single best decision governing ``fqn``, or ``None``.

    Ranks exact-FQN links (tier 1) above unique-short-name links (tier 2);
    ties break on the decision qn for deterministic output.
    """
    fqn = (fqn or "").strip()
    if not fqn:
        return None
    edges = store.governs_edges_for_symbol(fqn)
    if not edges:
        return None
    edges.sort(key=lambda e: (e.resolution_tier or 99, e.src))
    best = edges[0]
    row = _decision_row(store, best.src, span=best.span, tier=best.resolution_tier or None)
    if row is None:
        # The governing chunk was deleted after the edge was derived — fall
        # back to the next-best link rather than reporting a dangling row.
        for candidate in edges[1:]:
            row = _decision_row(
                store, candidate.src, span=candidate.span, tier=candidate.resolution_tier or None
            )
            if row is not None:
                break
    return row


def related_decisions(
    store: CodeGraphStore,
    searcher: CodeSearcher,
    query: str,
    *,
    k: int = 10,
) -> list[dict[str, Any]]:
    """Decisions relevant to ``query``, ranked and hydrated to ``k`` rows.

    A decision chunk that the search matches directly earns the hit's score;
    a decision that governs a matched code symbol earns the hit's score times
    ``_GOVERNS_CREDIT``. Higher score first, ties on qn.
    """
    query = (query or "").strip()
    if not query:
        return []

    pool = searcher.search(query, k=max(k * 3, 30))
    scores: dict[str, float] = {}

    def bump(qn: str, score: float) -> None:
        if qn:
            scores[qn] = max(scores.get(qn, 0.0), score)

    for hit in pool:
        if hit.kind == "MarkdownDoc":
            bump(hit.qualified_name, hit.score)
            continue
        for edge in store.governs_edges_for_symbol(hit.qualified_name):
            bump(edge.src, hit.score * _GOVERNS_CREDIT)

    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[: max(1, k)]
    out: list[dict[str, Any]] = []
    for qn, score in ranked:
        row = _decision_row(store, qn)
        if row is None:
            continue
        row["score"] = round(score, 4)
        out.append(row)
    return out


def entity_edges(
    store: CodeGraphStore,
    fqn: str,
    *,
    kind: str | None = None,
) -> list[dict[str, Any]]:
    """Typed entity edges around ``fqn`` (a code symbol or a decision chunk),
    both directions, optionally filtered by edge ``kind``.

    ``direction`` is from ``fqn``'s perspective: ``"in"`` for edges pointing
    at it (the far endpoint is ``other``), ``"out"`` for edges it emits. The
    far endpoint is hydrated; one that resolved to nothing is reported with
    ``resolved: false`` (dangling), not dropped.
    """
    fqn = (fqn or "").strip()
    if not fqn:
        return []
    wanted = (kind or "").upper() or None

    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def add(edge_kind: str, direction: str, other_qn: str, span: str | None, tier: int) -> None:
        if wanted and edge_kind != wanted:
            return
        key = (edge_kind, direction, other_qn)
        if other_qn in ("", "<standalone>") or key in seen:
            return
        seen.add(key)
        sym = store.symbol(other_qn) if other_qn else None
        rows.append(
            {
                "kind": edge_kind,
                "direction": direction,
                "source": other_qn if direction == "in" else fqn,
                "target": fqn if direction == "in" else other_qn,
                "resolved": sym is not None,
                "file": sym.file_path if sym else None,
                "line": sym.start_line if sym else None,
                "span": span,
                "resolution_tier": tier,
            }
        )

    # Incoming entity edges (which docs/contracts touch-require-constrain fqn).
    for e in store.typed_edges_for_fqn(fqn):
        add(e.kind, "in", e.src, e.span, e.resolution_tier or 0)
    # Outgoing entity edges (fqn is a chunk declaring its own targets).
    for e in store.typed_edges_from_chunks([fqn], limit=_ENTITY_LIMIT):
        add(e.kind, "out", e.dst, e.span, e.resolution_tier or 0)
    # Governance, both directions (chunks govern code; a symbol is governed by).
    for e in store.governs_edges_for_symbol(fqn):
        add("GOVERNS", "in", e.src, e.span, e.resolution_tier or 0)
    for e in store.governs_edges_from(fqn):
        add("GOVERNS", "out", e.dst, e.span, e.resolution_tier or 0)

    rows.sort(key=lambda r: (r["kind"], r["direction"], r["source"], r["target"]))
    return rows[:_ENTITY_LIMIT]
