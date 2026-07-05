"""Task-seeded context bundles: search + 1-hop call graph + budgeted source.

Rewrites the essence of codebase-indexer's ``routers/context_bundle.py``:

- Seed via :func:`~agentalloy.code_index.retrieval.hybrid.semantic_search`.
- Expand the top seeds one hop through the CALLS graph (callers + callees,
  capped per seed) so the consumer sees what the seeds touch.
- Score: seeds keep their search score; expansion neighbours inherit a
  decayed fraction (0.5x); test/spec paths are down-weighted 0.4x
  (mirroring TheForge's orchestrator-side test-path multiplier).
- Budget truncation: greedily take the highest-scored symbols' source until
  ``budget_chars`` is spent. Every included item always carries its
  qualified_name / file_path / line header fields; only the source excerpt
  is truncated or dropped.

Deliberately NOT ported from the source: intent classification, module-
keyword boosts, entry-point boosting, summary-chunk hydration, refill passes
— that machinery compensated for weak seeds; the hybrid pipeline plus a
simple decay covers the agent-facing contract here.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

from agentalloy.code_index.retrieval.hybrid import semantic_search
from agentalloy.code_index.store import open_code_index

if TYPE_CHECKING:
    from agentalloy.code_index.api.state import CodeIndexState

_SEED_K = 20
"""Seeds requested from semantic search."""

_EXPAND_SEEDS = 5
"""Only the strongest seeds are expanded through the call graph."""

_NEIGHBOR_CAP = 8
"""Max callers and max callees admitted per expanded seed."""

_EXPANSION_DECAY = 0.5
"""A neighbour inherits this fraction of its seed's score."""

_TEST_PATH_PENALTY = 0.4
"""Multiplicative down-weight for symbols living under test/spec paths."""

_TEST_PATH_MARKERS = ("/tests/", "test_", "_test", "/spec/")

Reason = Literal["seed", "caller", "callee"]


class BundleItem(BaseModel):
    """One symbol in the bundle. Header fields are always populated; only
    ``source`` is subject to budget truncation."""

    qualified_name: str
    file_path: str | None
    start_line: int | None
    end_line: int | None
    score: float
    reason: Reason
    source: str


class Bundle(BaseModel):
    """POST /code/context-bundle response body."""

    repo: str
    task: str
    budget_chars: int
    total_chars: int
    seed_count: int
    items: list[BundleItem]


def _is_test_path(qualified_name: str, file_path: str | None) -> bool:
    """Substring match on the file path (falls back to the dotted FQN mapped
    to path form so registry-less symbols still get classified)."""
    probe = (file_path or qualified_name.replace(".", "/")).lower()
    return any(marker in probe for marker in _TEST_PATH_MARKERS)


async def build_bundle(
    state: CodeIndexState, slug: str, task: str, *, budget_chars: int = 24000
) -> Bundle:
    """Assemble a budgeted context bundle for ``task`` (see module docstring)."""
    seeds = await semantic_search(state, slug, task, k=_SEED_K)

    def _assemble() -> Bundle:
        handles = open_code_index(state.settings, slug, role="service")
        try:
            graph = handles.graph

            # score + reason per candidate; seeds win ties, higher score wins.
            candidates: dict[str, tuple[float, Reason]] = {}

            def _admit(qn: str, score: float, reason: Reason, file_path: str | None) -> None:
                if _is_test_path(qn, file_path):
                    score *= _TEST_PATH_PENALTY
                prev = candidates.get(qn)
                if prev is None or prev[0] < score or (prev[1] != "seed" and reason == "seed"):
                    # A symbol that is both a seed and a neighbour stays a seed.
                    if prev is not None and prev[1] == "seed":
                        reason = "seed"
                        score = max(score, prev[0])
                    candidates[qn] = (score, reason)

            for seed in seeds:
                _admit(seed.qualified_name, seed.score, "seed", seed.file_path)

            for seed in seeds[:_EXPAND_SEEDS]:
                base = candidates[seed.qualified_name][0] * _EXPANSION_DECAY
                for site in graph.callers(seed.qualified_name)[:_NEIGHBOR_CAP]:
                    _admit(site.qualified_name, base, "caller", site.file_path)
                for site in graph.callees(seed.qualified_name)[:_NEIGHBOR_CAP]:
                    _admit(site.qualified_name, base, "callee", site.file_path)

            ranked = sorted(candidates.items(), key=lambda item: (-item[1][0], item[0]))

            items: list[BundleItem] = []
            total = 0
            for qn, (score, reason) in ranked:
                sym = graph.symbol(qn)
                if sym is None:
                    continue  # dangling edge endpoint (unresolved external)
                source = sym.source_code or ""
                header_cost = len(qn) + len(sym.file_path or "") + 24
                if total + header_cost > budget_chars:
                    break
                room = budget_chars - total - header_cost
                if len(source) > room:
                    source = source[:room]
                total += header_cost + len(source)
                items.append(
                    BundleItem(
                        qualified_name=qn,
                        file_path=sym.file_path,
                        start_line=sym.start_line,
                        end_line=sym.end_line,
                        score=score,
                        reason=reason,
                        source=source,
                    )
                )
                if total >= budget_chars:
                    break

            return Bundle(
                repo=slug,
                task=task,
                budget_chars=budget_chars,
                total_chars=total,
                seed_count=len(seeds),
                items=items,
            )
        finally:
            handles.close()

    return await asyncio.to_thread(_assemble)
