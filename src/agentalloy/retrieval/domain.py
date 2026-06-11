"""Domain fragment retrieval pipeline.

Given a task + phase + optional filters, embed the task via the inference
runtime, query DuckDB ``fragment_embeddings`` for top-k by cosine, fuse with
a BM25 lexical leg via Reciprocal Rank Fusion (RRF), hydrate
ActiveFragment metadata from LadybugDB, then apply skill-granular selection
to prevent sibling skills from crowding out unrelated relevant skills.

Per v5.3, vector storage is DuckDB; cosine ranking happens in DuckDB via
``array_cosine_distance`` over L2-normalized vectors.

Improvements (v5.4+):
- Rule-based keyword extraction boosts BM25 lexical recall.
- Phase-specific RRF weighting allows biasing dense vs. lexical legs.

Improvements (v5.5 — Stage B):
- Skill-granular selection: round-robin over top-k skills prevents sibling
  skills (near-duplicate fragments) from flooding the selected set and
  crowding out a third relevant skill.
"""

from __future__ import annotations

import logging
import os as _os
import re as _re
import time
from dataclasses import dataclass, field
from typing import Protocol, TypedDict, runtime_checkable

from agentalloy.api.compose_models import Phase
from agentalloy.embed_provider import EmbedClient
from agentalloy.reads import ActiveFragment
from agentalloy.reads.models import SkillClass
from agentalloy.retrieval.embedding_errors import (
    EmbeddingError,
    EmbeddingErrorCode,
    EmbeddingErrorResult,
    embedding_breaker,
    safe_embed,
)
from agentalloy.retrieval.rerank import build_reranker_from_env, rerank_max_pairs
from agentalloy.storage.vector_store import SimilarityHit, VectorStore

_RRF_K_DEFAULT = 60
logger = logging.getLogger(__name__)
_DEGRADABLE_EMBEDDING_CODES = {
    EmbeddingErrorCode.CIRCUIT_OPEN,
    EmbeddingErrorCode.UNAVAILABLE,
    EmbeddingErrorCode.TIMEOUT,
    EmbeddingErrorCode.BAD_RESPONSE,
}


class _RRFConfig(TypedDict):
    k: int
    dense_weight: float
    bm25_weight: float


# Phase -> RRF configuration: k value, dense weight, bm25 weight.
# Adjusting weights allows biasing retrieval towards semantic (dense) or lexical (bm25) matches.
_PHASE_RRF_CONFIG: dict[str, _RRFConfig] = {
    "default": {"k": _RRF_K_DEFAULT, "dense_weight": 1.0, "bm25_weight": 1.0},
    "qa": {"k": _RRF_K_DEFAULT, "dense_weight": 0.8, "bm25_weight": 1.2},
    "spec": {"k": _RRF_K_DEFAULT, "dense_weight": 1.2, "bm25_weight": 0.8},
}

# Regex to extract high-signal technical terms for BM25 boosting.
# Matches: file extensions, CamelCase classes, snake_case functions, version numbers, common tech terms.
_TECH_KEYWORD_RE = _re.compile(
    r"\b(?:\.\w{2,4}|[A-Z][a-z]+\w*|[a-z_]+\d+\w*|[a-z]+-[a-z]+|[A-Z]{2,})\b",
    _re.IGNORECASE,
)


def _get_rrf_params(phase: Phase) -> tuple[int, float, float]:
    """Return phase-specific RRF parameters (k, dense_weight, bm25_weight)."""
    cfg = _PHASE_RRF_CONFIG.get(phase, _PHASE_RRF_CONFIG["default"])
    return cfg["k"], cfg["dense_weight"], cfg["bm25_weight"]


def _extract_bm25_keywords(task: str) -> str:
    """Extract high-signal technical terms and append them to the query for BM25 boosting."""
    matches = list(dict.fromkeys(_TECH_KEYWORD_RE.findall(task)))
    if matches:
        return f"{task} {' '.join(matches)}"
    return task


def _resolve_bm25_query(task: str, contract_tags: list[str] | None) -> tuple[str, str]:
    """Resolve the BM25 query text and telemetry source label."""
    if contract_tags:
        bm25_query = " ".join(contract_tags)
        if _os.environ.get("AGENTALLOY_UNION_KEYWORDS") == "1":
            return f"{bm25_query} {_extract_bm25_keywords(task)}", "union"
        return bm25_query, "contract"
    return _extract_bm25_keywords(task), "rule-extracted"


@runtime_checkable
class FragmentSource(Protocol):
    """Structural protocol satisfied by ``RuntimeCache`` and ``StoreFragmentSource``."""

    def get_active_fragments(
        self,
        *,
        skill_class: SkillClass | tuple[str, ...] | None = None,
        categories: list[str] | None = None,
        phases: list[str] | None = None,
        domain_tags: list[str] | None = None,
    ) -> list[ActiveFragment]: ...

    def get_deprecated_skill_ids(self) -> list[str]: ...


# Phase -> eligible-category mapping. Aligned with the seeded corpus
# vocabulary (design, engineering, quality, review, tooling, ops). The
# legacy phase-as-category vocabulary (spec/qa/build/governance/meta) is
# kept where it overlaps so existing fragments authored to that schema
# remain reachable.
_PHASE_TO_CATEGORIES: dict[Phase, list[str]] = {
    "spec": ["spec", "design", "tooling", "governance", "meta"],
    "design": ["design", "engineering", "tooling", "governance", "meta"],
    # "design" in qa/ops for the same reason as build (below): protocol-
    # convention packs (webhooks, REST, ...) are category=design, and
    # review/incident tasks need them. Measured: qa-phase "review this
    # webhook receiver" retrieved redis/nodejs noise instead of
    # webhooks-signature-verification before this line.
    "qa": ["qa", "quality", "review", "design", "engineering", "tooling", "governance", "meta"],
    # "design" is included for build: domain packs author API/protocol
    # convention skills (webhooks, REST, ...) as category=design with
    # phase_scope=[build] — excluding the category made 494 fragments
    # unreachable during the most common agent phase (measured via the
    # domain benchmark: gold-skill retrieval missed on every build-phase
    # webhook task, hit on every design-phase one).
    "build": ["build", "design", "engineering", "tooling", "ops", "governance", "meta"],
    "ops": ["ops", "design", "engineering", "tooling", "governance", "meta"],
    "meta": ["meta", "tooling", "governance"],
    "governance": ["governance", "review", "quality", "meta"],
}

# Order of preference for structural diversity during reshuffle.
_DIVERSITY_PRIORITY: tuple[str, ...] = ("setup", "execution", "verification")


def phase_to_categories(phase: Phase) -> list[str]:
    """Return the ordered list of Skill.category values eligible for a given phase."""
    return list(_PHASE_TO_CATEGORIES[phase])


# Runtime phase -> authored phase_scope vocabulary. Authors write
# phase_scope in {build, design, review}; "review" maps to the qa and
# governance runtime phases. Phases with no authored vocabulary (meta)
# rely on the category map alone.
_PHASE_SCOPE_TERMS: dict[Phase, list[str]] = {
    "spec": ["spec"],
    "design": ["design"],
    "build": ["build"],
    "qa": ["qa", "review"],
    "ops": ["ops"],
    "meta": [],
    "governance": ["governance", "review"],
}


def phase_to_scope_terms(phase: Phase) -> list[str]:
    """Authored phase_scope values that admit a fragment for *phase*.

    Eligibility is the UNION of this and ``phase_to_categories`` — the
    authored scope can only widen what the category map admits (it never
    narrows), so skills whose category/phase metadata disagree (e.g. the
    testing pack: category=quality, phase_scope=[build]) stay reachable
    in their authored phases. Measured by eval/retrieval_audit.py: the
    category map alone stranded 48 skills (quality/review hit rate 0.00).
    """
    return list(_PHASE_SCOPE_TERMS.get(phase, []))


@dataclass(frozen=True)
class RetrievalResult:
    candidates: list[ActiveFragment]
    eligible_count: int
    retrieval_ms: int
    # cosine similarity per fragment_id (in [0, 1]); 1 = identical direction.
    scores_by_id: dict[str, float] = field(default_factory=lambda: {})
    bm25_source: str = "rule-extracted"  # "rule-extracted" | "contract" | "union"
    # Skill IDs ordered by rank (first fragment appearance) — observability for Stage B.
    skills_ranked: list[str] = field(default_factory=lambda: [])
    # True only when a cross-encoder rerank actually reordered the pool (Stage A).
    reranked: bool = False


class StoreFragmentSource:
    """Thin adapter so a raw ``LadybugStore`` satisfies ``FragmentSource``."""

    def __init__(self, store: object) -> None:
        self._store = store

    def get_active_fragments(
        self,
        *,
        skill_class: SkillClass | tuple[str, ...] | None = None,
        categories: list[str] | None = None,
        phases: list[str] | None = None,
        domain_tags: list[str] | None = None,
    ) -> list[ActiveFragment]:
        from agentalloy.reads import get_active_fragments  # local import avoids cycle

        return get_active_fragments(
            self._store,  # type: ignore[arg-type]
            skill_class=skill_class,
            categories=categories,
            phases=phases,
            domain_tags=domain_tags,
        )

    def get_deprecated_skill_ids(self) -> list[str]:
        from agentalloy.reads import get_deprecated_skill_ids  # local import avoids cycle

        return get_deprecated_skill_ids(self._store)  # type: ignore[arg-type]


def _rrf_fuse(
    dense_hits: list[SimilarityHit],
    bm25_fragment_ids: list[str],
    k: int = _RRF_K_DEFAULT,
    *,
    dense_weight: float = 1.0,
    bm25_weight: float = 1.0,
) -> list[str]:
    """Reciprocal Rank Fusion over dense and BM25 result lists.

    Returns fragment_ids ordered by descending RRF score. Documents appearing
    in only one leg get a rank of len(that_leg)+1 in the missing leg.
    Applies configurable weights to bias towards semantic or lexical matches.
    """
    dense_ids = [h.fragment_id for h in dense_hits]
    all_ids = dict.fromkeys(dense_ids + bm25_fragment_ids)

    dense_rank = {fid: i + 1 for i, fid in enumerate(dense_ids)}
    bm25_rank = {fid: i + 1 for i, fid in enumerate(bm25_fragment_ids)}
    dense_miss = len(dense_ids) + 1
    bm25_miss = len(bm25_fragment_ids) + 1

    scores: dict[str, float] = {}
    for fid in all_ids:
        dense_score = dense_weight * (1.0 / (k + dense_rank.get(fid, dense_miss)))
        bm25_score = bm25_weight * (1.0 / (k + bm25_rank.get(fid, bm25_miss)))
        scores[fid] = dense_score + bm25_score

    return sorted(all_ids, key=lambda fid: scores[fid], reverse=True)


def _bm25_fallback_result(
    frag_src: FragmentSource,
    vector_store: VectorStore,
    *,
    task: str,
    phase: Phase,
    domain_tags: list[str] | None,
    k: int,
    raw_scores: bool,
    contract_tags: list[str] | None,
    error: EmbeddingError,
    start_ns: int,
) -> EmbeddingErrorResult:
    """Run the lexical leg only and package the degraded retrieval result."""
    categories = phase_to_categories(phase)
    scope_terms = phase_to_scope_terms(phase)
    pool_size = max(k * 2, 50)
    deprecated_ids = frag_src.get_deprecated_skill_ids()
    bm25_query, bm25_source = _resolve_bm25_query(task, contract_tags)
    bm25_hits = vector_store.search_bm25(
        bm25_query,
        categories=categories,
        phases=scope_terms or None,
        deprecated_skill_ids=deprecated_ids,
        k=pool_size,
    )

    metadata = frag_src.get_active_fragments(
        skill_class=("domain", "workflow"),
        categories=categories,
        phases=scope_terms or None,
        domain_tags=domain_tags,
    )
    by_id = {f.fragment_id: f for f in metadata}

    ranked: list[ActiveFragment] = []
    scores_by_id: dict[str, float] = {}
    for hit in bm25_hits:
        frag = by_id.get(hit.fragment_id)
        if frag is None:
            continue
        ranked.append(frag)
        scores_by_id[hit.fragment_id] = hit.score

    eligible_count = len(ranked)
    diversity_off = _os.environ.get("RUNTIME_DIVERSITY_SELECTION", "on").lower() == "off"
    if raw_scores or diversity_off:
        selected = ranked[:k]
    else:
        selected, _ = skill_granular_select(ranked, k)
    elapsed_ms = int((time.perf_counter_ns() - start_ns) // 1_000_000)
    return EmbeddingErrorResult(
        error=error,
        bm25_only=True,
        candidates=selected,
        eligible_count=eligible_count,
        retrieval_ms=elapsed_ms,
        scores_by_id=scores_by_id,
        bm25_source=bm25_source,
    )


def retrieve_domain_candidates(
    source: object,
    lm: EmbedClient,
    vector_store: VectorStore,
    *,
    task: str,
    phase: Phase,
    domain_tags: list[str] | None,
    k: int,
    embedding_model: str,
    raw_scores: bool = False,
    contract_tags: list[str] | None = None,
) -> RetrievalResult | EmbeddingErrorResult:
    """Execute the retrieval pipeline and return a bounded candidate set.

    ``source`` may be a ``RuntimeCache`` (startup-loaded snapshot) or a raw
    ``LadybugStore`` (wrapped automatically via ``StoreFragmentSource``).
    ``vector_store`` is a DuckDB ``VectorStore`` whose ``fragment_embeddings``
    table is populated via the reembed CLI.

    Stages:

    1. Check circuit breaker — if open, skip embedding and return BM25-only
    2. embed the task via ``safe_embed`` (propagates EmbeddingError on failure)
    3. DuckDB top-k vector search filtered by phase categories
    4. DuckDB BM25 search on prose column filtered by phase categories (with keyword extraction)
    5. Reciprocal Rank Fusion of both legs (with phase-specific weighting)
    6. hydrate ActiveFragment metadata from ``source`` and apply optional
       domain_tags filter
    7. skill-granular selection — round-robin over top-k skills so sibling
       skills cannot flood the selected set; within each skill, diversity
       preference (setup/execution/verification) applies across the global
       selected set (skipped when ``raw_scores=True``)

    Returns:
        RetrievalResult on success, EmbeddingErrorResult when the embedding
        service is unavailable (circuit open or call failed). The caller
        (compose.py) should treat this as a partial result and proceed with
        BM25-only fragments if available.
    """
    start_ns = time.perf_counter_ns()

    frag_src: FragmentSource = (
        source if isinstance(source, FragmentSource) else StoreFragmentSource(source)
    )

    # ------------------------------------------------------------------
    # Stage 1: Circuit breaker check — skip embedding if circuit is open
    # ------------------------------------------------------------------
    if not embedding_breaker.allow_request():
        logger.warning(
            "embedding circuit open for task=%s phase=%s; falling back to BM25-only",
            task[:80],
            phase,
        )
        return _bm25_fallback_result(
            frag_src,
            vector_store,
            task=task,
            phase=phase,
            domain_tags=domain_tags,
            k=k,
            raw_scores=raw_scores,
            contract_tags=contract_tags,
            error=EmbeddingError(
                EmbeddingErrorCode.CIRCUIT_OPEN,
                message="circuit breaker open — embedding unavailable",
            ),
            start_ns=start_ns,
        )

    # ------------------------------------------------------------------
    # Stage 2: Safe embedding with circuit-breaker integration
    # ------------------------------------------------------------------
    try:
        task_description = (
            "Given a software engineering task description, retrieve relevant "
            "skill instruction fragments"
        )
        embed_input = f"Instruct: {task_description}\nQuery:{task}"
        query_vec = safe_embed(lm, embedding_model, [embed_input])[0]
    except EmbeddingError as exc:
        if exc.code not in _DEGRADABLE_EMBEDDING_CODES:
            if exc.original is not None:
                raise exc.original from exc
            raise
        logger.warning(
            "embedding failed for task=%s phase=%s code=%s: %s",
            task[:80],
            phase,
            exc.code.value,
            exc.message,
        )
        return _bm25_fallback_result(
            frag_src,
            vector_store,
            task=task,
            phase=phase,
            domain_tags=domain_tags,
            k=k,
            raw_scores=raw_scores,
            contract_tags=contract_tags,
            error=exc,
            start_ns=start_ns,
        )

    categories = phase_to_categories(phase)
    scope_terms = phase_to_scope_terms(phase)
    pool_size = max(k * 2, 50)
    deprecated_ids = frag_src.get_deprecated_skill_ids()

    dense_hits = vector_store.search_similar(
        query_vec,
        categories=categories,
        phases=scope_terms or None,
        deprecated_skill_ids=deprecated_ids,
        k=pool_size,
    )

    # BM25 query: contract tags take priority over rule-extracted keywords.
    # The paid LLM picked them deliberately; they're better keywords than
    # rule-extracted ones. Union mode enabled by AGENTALLOY_UNION_KEYWORDS=1.

    bm25_query, _bm25_source = _resolve_bm25_query(task, contract_tags)
    bm25_hits = vector_store.search_bm25(
        bm25_query,
        categories=categories,
        phases=scope_terms or None,
        deprecated_skill_ids=deprecated_ids,
        k=pool_size,
    )
    bm25_ids = [h.fragment_id for h in bm25_hits]

    if not dense_hits and not bm25_hits:
        elapsed_ms = (time.perf_counter_ns() - start_ns) // 1_000_000
        return RetrievalResult(
            candidates=[], eligible_count=0, retrieval_ms=int(elapsed_ms), bm25_source=_bm25_source
        )

    # Apply phase-specific RRF weights
    rrf_k, dense_weight, bm25_weight = _get_rrf_params(phase)
    fused_ids = _rrf_fuse(
        dense_hits, bm25_ids, k=rrf_k, dense_weight=dense_weight, bm25_weight=bm25_weight
    )

    # Hydrate ActiveFragment metadata from the source. Pull domain fragments
    # for the eligible categories; intersect with the fused ids.
    metadata = frag_src.get_active_fragments(
        skill_class=("domain", "workflow"),
        categories=categories,
        phases=scope_terms or None,
        domain_tags=domain_tags,
    )
    by_id = {f.fragment_id: f for f in metadata}

    # Build dense score lookup for observability.
    dense_score_by_id = {h.fragment_id: 1.0 - h.distance for h in dense_hits}

    ranked: list[ActiveFragment] = []
    scores_by_id: dict[str, float] = {}
    for fid in fused_ids:
        frag = by_id.get(fid)
        if frag is None:
            continue
        ranked.append(frag)
        scores_by_id[fid] = dense_score_by_id.get(fid, 0.0)

    # domain_tags is a post-retrieval filter per the API contract: it narrows
    # the fused candidate set and may legitimately empty it. It cannot recruit
    # tag-matching skills the search itself missed — when that happens the
    # response is empty even though matching skills exist in the corpus, so
    # make it loud for operators.
    if domain_tags and not ranked and fused_ids:
        logger.warning(
            "domain_tags %s filtered out all %d fused candidates for task=%s "
            "phase=%s — tag-matching skills may exist but were not retrieved",
            domain_tags,
            len(fused_ids),
            task[:80],
            phase,
        )

    eligible_count = len(ranked)

    # raw_scores=True: return pre-diversity order (for /retrieve observability).
    # RUNTIME_DIVERSITY_SELECTION=off also short-circuits — used by eval harness.
    diversity_off = _os.environ.get("RUNTIME_DIVERSITY_SELECTION", "on").lower() == "off"
    reranked = False
    if raw_scores or diversity_off:
        selected = ranked[:k]
        skills_ranked: list[str] = []
    else:
        # Stage A: cross-encoder rerank of the top skills before selection.
        # Best-effort — a failure or disabled stage leaves ``ranked`` untouched.
        ranked, reranked = _maybe_rerank(ranked, task)
        selected, skills_ranked = skill_granular_select(ranked, k)

    elapsed_ms = (time.perf_counter_ns() - start_ns) // 1_000_000
    return RetrievalResult(
        candidates=selected,
        eligible_count=eligible_count,
        retrieval_ms=int(elapsed_ms),
        scores_by_id=scores_by_id,
        bm25_source=_bm25_source,
        skills_ranked=skills_ranked,
        reranked=reranked,
    )


def _maybe_rerank(ranked: list[ActiveFragment], task: str) -> tuple[list[ActiveFragment], bool]:
    """Reorder ``ranked`` by cross-encoder relevance over the top skills.

    Groups fragments by skill (rank order; dict insertion order gives this for
    free, same trick as ``skill_granular_select``), takes the top
    ``RUNTIME_RERANK_MAX_PAIRS`` skills, scores each skill's best fragment
    against ``task``, and reorders those skills by score descending (stable for
    ties). Skills beyond the cap keep their original relative order after the
    reranked ones. Within-skill fragment order is preserved throughout.

    Returns ``(ranked, reranked)`` where ``reranked`` is True only when the
    scorer succeeded and produced a reordering input. Any failure, disabled
    stage, or trivial pool returns the input list unchanged with False — this
    function never raises.
    """
    reranker = build_reranker_from_env()
    if reranker is None:
        return ranked, False

    # Group into per-skill fragment queues in rank order.
    skill_queues: dict[str, list[ActiveFragment]] = {}
    for frag in ranked:
        skill_queues.setdefault(frag.skill_id, []).append(frag)
    if len(skill_queues) < 2:
        return ranked, False

    skill_ids = list(skill_queues.keys())
    cap = rerank_max_pairs()
    head_ids = skill_ids[:cap]
    tail_ids = skill_ids[cap:]
    # Passage = skill identity + best fragment. Fragment prose alone often
    # omits the skill's name/topic, which is exactly what short queries carry —
    # measured: reranking over bare content scored below the un-reranked order.
    passages = [f"{sid.replace('-', ' ')}: {skill_queues[sid][0].content}" for sid in head_ids]

    try:
        scores = reranker.score(task, passages)
    except Exception:  # pyright: ignore[reportBroadExceptionCaught]
        # The latched reranker already swallows scorer errors, but guard the
        # call site too — reranking must never break retrieval.
        logger.warning("rerank stage raised unexpectedly; using un-reranked order")
        return ranked, False

    if len(scores) != len(head_ids):
        return ranked, False

    # Stable sort by score descending preserves original order for ties.
    order = sorted(range(len(head_ids)), key=lambda i: scores[i], reverse=True)
    new_skill_order = [head_ids[i] for i in order] + tail_ids

    rebuilt: list[ActiveFragment] = []
    for sid in new_skill_order:
        rebuilt.extend(skill_queues[sid])
    return rebuilt, True


def diversity_select(pool: list[ActiveFragment], k: int) -> list[ActiveFragment]:
    """Greedy selection that favors unseen fragment_types from the priority set.

    When a priority type (setup, execution, verification) is not yet represented
    in ``selected``, prefer the highest-scoring candidate of that type. Otherwise
    fall back to the next highest-scoring candidate regardless of type. Already-
    selected fragments are never re-picked.
    """
    selected: list[ActiveFragment] = []
    selected_types: set[str] = set()
    # `pool` is already ranked by similarity — index preserves score order.
    remaining = list(pool)

    while len(selected) < k and remaining:
        chosen_index: int | None = None
        # First pass: pick a priority type not yet selected.
        for ptype in _DIVERSITY_PRIORITY:
            if ptype in selected_types:
                continue
            for i, frag in enumerate(remaining):
                if frag.fragment_type == ptype:
                    chosen_index = i
                    break
            if chosen_index is not None:
                break
        # Fallback: take the top-ranked remaining fragment.
        if chosen_index is None:
            chosen_index = 0
        frag = remaining.pop(chosen_index)
        selected.append(frag)
        selected_types.add(frag.fragment_type)

    return selected


def skill_granular_select(
    ranked: list[ActiveFragment], k: int
) -> tuple[list[ActiveFragment], list[str]]:
    """Round-robin selection across skills to prevent sibling-skill cannibalization.

    Groups fragments by skill_id in rank order (dict insertion order gives this
    for free — each skill's rank = its first fragment's position in ``ranked``).
    Then issues k tokens via round-robin: pass 1 gives one fragment to each
    top skill, pass 2 gives a second, until k fragments are selected or all
    queues are empty.

    Within each skill's queue, prefer a ``fragment_type`` from
    ``_DIVERSITY_PRIORITY`` not yet represented in the globally selected set
    (same logic as ``diversity_select``); otherwise take the skill's next
    highest-ranked fragment.

    Returns:
        (selected, skills_ranked) where ``skills_ranked`` is the ordered list
        of all distinct skill_ids in rank order (not just the winning ones).

    Degenerate cases:
    - Empty input → ([], []).
    - Fewer distinct skills than k → later passes fill remaining slots from
      whichever skills still have fragments.
    """
    if not ranked:
        return [], []

    # Group fragments by skill_id, preserving fragment rank order within each group.
    skill_queues: dict[str, list[ActiveFragment]] = {}
    for frag in ranked:
        if frag.skill_id not in skill_queues:
            skill_queues[frag.skill_id] = []
        skill_queues[frag.skill_id].append(frag)

    # skills_ranked = insertion order = rank order of first fragment per skill.
    skills_ranked: list[str] = list(skill_queues.keys())
    # Working queues — mutated during selection; list copy so original is unchanged.
    queues: dict[str, list[ActiveFragment]] = {
        sid: list(frags) for sid, frags in skill_queues.items()
    }

    selected: list[ActiveFragment] = []
    selected_types: set[str] = set()

    while len(selected) < k:
        made_progress = False
        for sid in skills_ranked:
            if len(selected) >= k:
                break
            queue = queues[sid]
            if not queue:
                continue
            # Prefer a priority type not yet in the globally selected set.
            chosen_index: int | None = None
            for ptype in _DIVERSITY_PRIORITY:
                if ptype in selected_types:
                    continue
                for i, frag in enumerate(queue):
                    if frag.fragment_type == ptype:
                        chosen_index = i
                        break
                if chosen_index is not None:
                    break
            if chosen_index is None:
                chosen_index = 0
            frag = queue.pop(chosen_index)
            selected.append(frag)
            selected_types.add(frag.fragment_type)
            made_progress = True
        # All queues exhausted before k fragments were gathered.
        if not made_progress:
            break

    return selected, skills_ranked
