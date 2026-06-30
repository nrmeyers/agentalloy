"""DuckDB-backed dedup classifier for the QA gate's near-duplicate check.

This is the v1.5 replacement for the ``run_dedup`` path in ``qa_gate.py`` that
currently queries the fragment store directly. When NXS-799 lands,
``qa_gate.run_dedup`` becomes a thin wrapper around :func:`dedup_candidates`.

For now it lives alongside the old path so tests can exercise it in isolation
without touching code the in-flight authoring batch depends on.

## Threshold semantics

Callers pass **similarity** thresholds in [0, 1]. This module converts to
**cosine distance** (``1 - similarity`` for L2-normalized vectors) to match
the fragment store's cosine-distance output.

- ``hard_similarity`` = 0.92 → any match with similarity ≥ 0.92 (distance ≤ 0.08)
  is a hard duplicate; auto-reject without Critic involvement.
- ``soft_similarity`` = 0.80 → matches in [0.80, 0.92) are handed to the Critic
  to rule on (genuinely distinct? cover same ground?).

## Batch embedding

The LM Studio embedding endpoint accepts an array of inputs, so all fragments
for a draft are embedded in ONE HTTP call. Saves a round-trip per fragment
and keeps the vector math consistent across the draft.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from agentalloy.dedup_gate import classify_hit as classify_hit  # re-export
from agentalloy.dedup_gate import dedup_fragment as _dedup_fragment_impl
from agentalloy.lm_client import OpenAICompatClient
from agentalloy.storage.protocols import FragmentStore, SimilarityHit

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DedupClassification:
    """Per-fragment classification against the existing corpus.

    ``hard`` is the closest match at or above the hard threshold (or None if
    no hit that close exists). ``soft`` is every other hit in the soft band.
    """

    label: str  # caller-provided identifier (e.g. "frag-2" or "raw_prose")
    hard: SimilarityHit | None
    soft: list[SimilarityHit] = field(default_factory=lambda: [])


@dataclass(frozen=True)
class DedupResult:
    """Aggregated dedup outcome across all fragments of a draft.

    ``hard`` is the single hardest hit across the draft (first reason to
    reject). ``soft`` is the concatenated, deduplicated list of near-matches
    for the Critic's review.
    """

    per_fragment: list[DedupClassification]
    hardest: SimilarityHit | None
    soft_all: list[SimilarityHit]

    @property
    def has_hard_duplicate(self) -> bool:
        return self.hardest is not None


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

# classify_hit is imported directly from dedup_gate (re-exported above) so
# callers that import it from this module continue to work unchanged.


def dedup_fragment(
    *,
    label: str,
    query_vec: Sequence[float],
    vector_store: FragmentStore,
    hard_similarity: float,
    soft_similarity: float,
    k: int = 20,
    categories: list[str] | None = None,
    fragment_types: list[str] | None = None,
) -> DedupClassification:
    """Search the fragment store for the top-k matches to one fragment, classify them.

    Thin wrapper around :func:`agentalloy.dedup_gate.dedup_fragment` that
    returns the authoring-layer :class:`DedupClassification` DTO.

    ``k`` caps how many neighbors we inspect per fragment; 20 is plenty for
    dedup decisions at the current corpus scale.
    """
    hard_match, soft_matches = _dedup_fragment_impl(
        label=label,
        query_vec=query_vec,
        vector_store=vector_store,
        hard_similarity=hard_similarity,
        soft_similarity=soft_similarity,
        k=k,
        categories=categories,
        fragment_types=fragment_types,
    )
    return DedupClassification(label=label, hard=hard_match, soft=soft_matches)


def dedup_candidates(
    *,
    labeled_contents: list[tuple[str, str]],
    embedder: OpenAICompatClient,
    vector_store: FragmentStore,
    embedding_model: str,
    hard_similarity: float,
    soft_similarity: float,
    k_per_fragment: int = 20,
    categories: list[str] | None = None,
    fragment_types: list[str] | None = None,
) -> DedupResult:
    """Embed and classify a draft's fragments against the corpus in one pass.

    ``labeled_contents`` is a list of ``(label, content)`` tuples — label is
    caller-owned (e.g. ``"frag-2"`` or ``"raw_prose"``), content is the text
    that'll be embedded. An empty list returns an empty result (no HTTP call).
    """
    if not labeled_contents:
        return DedupResult(per_fragment=[], hardest=None, soft_all=[])

    labels = [label for label, _ in labeled_contents]
    contents = [content for _, content in labeled_contents]
    vectors = embedder.embed(
        model=embedding_model, texts=[f"search_document: {c}" for c in contents]
    )

    per_fragment: list[DedupClassification] = []
    hardest: SimilarityHit | None = None
    soft_all: list[SimilarityHit] = []

    for label, vec in zip(labels, vectors, strict=True):
        classification = dedup_fragment(
            label=label,
            query_vec=vec,
            vector_store=vector_store,
            hard_similarity=hard_similarity,
            soft_similarity=soft_similarity,
            k=k_per_fragment,
            categories=categories,
            fragment_types=fragment_types,
        )
        per_fragment.append(classification)
        if classification.hard is not None and (
            hardest is None or classification.hard.distance < hardest.distance
        ):
            hardest = classification.hard
        soft_all.extend(classification.soft)

    # De-dup the soft list by fragment_id (the same existing fragment may show
    # up as a near-match for multiple candidate fragments).
    seen: set[str] = set()
    deduped_soft: list[SimilarityHit] = []
    for hit in sorted(soft_all, key=lambda h: h.distance):
        if hit.fragment_id in seen:
            continue
        seen.add(hit.fragment_id)
        deduped_soft.append(hit)

    return DedupResult(
        per_fragment=per_fragment,
        hardest=hardest,
        soft_all=deduped_soft,
    )
