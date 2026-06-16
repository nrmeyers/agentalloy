"""Reembed-boundary dedup gate — cross-pack near-duplicate detection.

Runs after new fragments have been written to DuckDB.  For each newly-embedded
skill, compares its fragments against every *other* skill already present in
the vector store and classifies matches as HARD (≥ hard_threshold) or SOFT
(≥ soft_threshold).

Same-pack deduplication is intentionally exempt: packs may contain sibling
skills that legitimately overlap.  "Same pack" is determined by the caller
passing the set of skill_ids that belong to the current ingest batch — any
match whose ``skill_id`` is in that set is treated as same-pack and ignored.

This module is strict-clean (pyright strict mode, no ``# type: ignore``).
``authoring/dedup.py`` imports :func:`classify_hit` and :func:`dedup_fragment`
from here so there is a single implementation.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from agentalloy.storage.vector_store import SimilarityHit, VectorStore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DedupMatch:
    """A single cross-pack near-duplicate finding for one new skill."""

    incoming_skill_id: str
    existing_skill_id: str
    fragment_id_incoming: str
    fragment_id_existing: str
    similarity: float
    verdict: str  # "hard" | "soft"


@dataclass
class DedupGateResult:
    """Aggregated findings from the dedup gate pass."""

    hard: list[DedupMatch] = field(default_factory=list)
    soft: list[DedupMatch] = field(default_factory=list)

    @property
    def has_hard(self) -> bool:
        return len(self.hard) > 0

    @property
    def has_soft(self) -> bool:
        return len(self.soft) > 0


# ---------------------------------------------------------------------------
# Core similarity helpers — shared with authoring/dedup.py
# ---------------------------------------------------------------------------


def classify_hit(
    hit: SimilarityHit,
    *,
    hard_similarity: float,
    soft_similarity: float,
) -> str:
    """Return ``"hard"``, ``"soft"``, or ``"ignore"`` for a single similarity hit.

    Convention: vectors in DuckDB are L2-normalized, so
    ``similarity = 1.0 - cosine_distance``.
    """
    similarity = 1.0 - hit.distance
    if similarity >= hard_similarity:
        return "hard"
    if similarity >= soft_similarity:
        return "soft"
    return "ignore"


def dedup_fragment(
    *,
    label: str,
    query_vec: Sequence[float],
    vector_store: VectorStore,
    hard_similarity: float,
    soft_similarity: float,
    k: int = 20,
    categories: list[str] | None = None,
    fragment_types: list[str] | None = None,
    exclude_fragment_id: str | None = None,
    exclude_skill_ids: set[str] | None = None,
) -> tuple[SimilarityHit | None, list[SimilarityHit]]:
    """Search DuckDB for top-k matches to *query_vec* and classify them.

    Returns ``(hard_match, soft_matches)`` where ``hard_match`` is the closest
    hit at or above ``hard_similarity`` (or ``None``), and ``soft_matches`` are
    all hits in the soft band.

    Unlike the authoring-layer wrapper this returns raw tuples so the gate can
    attach its own context (incoming/existing skill_ids) without extra DTOs.
    """
    hits = vector_store.search_similar(
        query_vec,
        k=k,
        categories=categories,
        fragment_types=fragment_types,
    )
    skip_skills = exclude_skill_ids or set()
    hard_match: SimilarityHit | None = None
    soft_matches: list[SimilarityHit] = []
    for hit in hits:
        # Exclude the fragment's own row and same-batch/same-pack hits BEFORE
        # picking the closest hard match. Dedup runs after the new fragments are
        # inserted, so the ~0-distance self-match would otherwise always win the
        # hard race and shadow a genuine cross-pack duplicate (which is then
        # filtered out downstream, silently dropping a real hard hit).
        if hit.fragment_id == exclude_fragment_id or hit.skill_id in skip_skills:
            continue
        verdict = classify_hit(
            hit, hard_similarity=hard_similarity, soft_similarity=soft_similarity
        )
        if verdict == "hard":
            if hard_match is None or hit.distance < hard_match.distance:
                hard_match = hit
        elif verdict == "soft":
            soft_matches.append(hit)

    _ = label  # label is accepted for call-site symmetry with authoring layer
    return hard_match, soft_matches


# ---------------------------------------------------------------------------
# Gate runner
# ---------------------------------------------------------------------------


def run_dedup_gate(
    *,
    new_skill_ids: set[str],
    new_fragment_vecs: dict[str, tuple[str, list[float]]],
    vector_store: VectorStore,
    hard_similarity: float,
    soft_similarity: float,
    k: int = 20,
) -> DedupGateResult:
    """Compare newly-embedded fragments against the existing corpus.

    Parameters
    ----------
    new_skill_ids:
        The set of ``skill_id`` values for skills whose fragments were just
        inserted in this reembed run.  Matches against any ``skill_id`` in
        this set are exempt (same-pack / same-batch rule).
    new_fragment_vecs:
        Mapping of ``fragment_id → (skill_id, embedding_vector)`` for every
        fragment that was newly embedded.  Vectors should be the raw
        (pre-normalisation) vectors returned by the embed call; the gate
        queries DuckDB which normalises internally, so consistency is maintained.
    vector_store:
        Open VectorStore (DuckDB) — must already contain the newly-inserted rows
        so that ``search_similar`` can find existing fragments by other skills.
    hard_similarity / soft_similarity:
        Thresholds (in [0, 1]) from ``Settings``.
    k:
        Neighbours to inspect per fragment.

    Returns
    -------
    DedupGateResult
        ``.hard`` — cross-pack hard duplicates (similarity ≥ hard_similarity).
        ``.soft`` — cross-pack near-duplicates (similarity ≥ soft_similarity).
        Same-pack hits (existing ``skill_id`` ∈ ``new_skill_ids``) are excluded.
    """
    result = DedupGateResult()

    for fragment_id, (incoming_skill_id, vec) in new_fragment_vecs.items():
        hard_hit, soft_hits = dedup_fragment(
            label=fragment_id,
            query_vec=vec,
            vector_store=vector_store,
            hard_similarity=hard_similarity,
            soft_similarity=soft_similarity,
            k=k,
            exclude_fragment_id=fragment_id,
            exclude_skill_ids=new_skill_ids,
        )

        # Self/same-pack hits are already excluded inside dedup_fragment; this
        # guard stays as defense-in-depth (and documents the same-pack rule).
        if (
            hard_hit is not None
            and hard_hit.fragment_id != fragment_id
            and hard_hit.skill_id not in new_skill_ids
        ):
            sim = round(1.0 - hard_hit.distance, 4)
            result.hard.append(
                DedupMatch(
                    incoming_skill_id=incoming_skill_id,
                    existing_skill_id=hard_hit.skill_id,
                    fragment_id_incoming=fragment_id,
                    fragment_id_existing=hard_hit.fragment_id,
                    similarity=sim,
                    verdict="hard",
                )
            )

        for soft_hit in soft_hits:
            if soft_hit.fragment_id != fragment_id and soft_hit.skill_id not in new_skill_ids:
                sim = round(1.0 - soft_hit.distance, 4)
                result.soft.append(
                    DedupMatch(
                        incoming_skill_id=incoming_skill_id,
                        existing_skill_id=soft_hit.skill_id,
                        fragment_id_incoming=fragment_id,
                        fragment_id_existing=soft_hit.fragment_id,
                        similarity=sim,
                        verdict="soft",
                    )
                )

    return result
