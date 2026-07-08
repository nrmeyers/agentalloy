"""Domain fragment retrieval pipeline.

Given a task + phase + optional filters, embed the task via the inference
runtime, query the Lance ``fragments`` dataset for top-k by cosine, fuse with
a BM25 lexical leg via Reciprocal Rank Fusion (RRF), hydrate
ActiveFragment metadata from the DuckDB skill store, then apply skill-granular
selection to prevent sibling skills from crowding out unrelated relevant skills.

In v5, vector storage is the Lance ``fragments`` dataset; cosine ranking
(ANN for retrieval, exact for dedup) happens inside LanceDB over L2-normalized
vectors.

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
from agentalloy.retrieval.lm_assist import (
    LMAssistOutcome,
    build_scorer_from_env,
    load_config,
    max_candidates,
)
from agentalloy.retrieval.query_bounds import build_retrieval_query
from agentalloy.retrieval.rerank import build_reranker_from_env, rerank_max_pairs
from agentalloy.storage.card_index import is_card_id, skill_id_from_card_id
from agentalloy.storage.protocols import FragmentStore, SimilarityHit

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


# Graph-expansion tuning. Off by default — flip RETRIEVAL_GRAPH_EXPAND=on to
# append REQUIRES_COMPOSITIONAL neighbors' fragments as trailing candidates.
_GRAPH_EXPAND_TOP_SKILLS = 3  # expand the top-N ranked skills
_GRAPH_EXPAND_MAX_FRAGMENTS = 2  # hard cap on appended fragments per request


def _graph_expand_enabled() -> bool:
    return _os.environ.get("RETRIEVAL_GRAPH_EXPAND", "off").lower() == "on"


# E4 — fused-score deepen-gate (ships inert). At 0.0 the gate is a strict no-op
# (legacy breadth-first selection); 0.85 is the recommended value after the
# post-#15 K sweep, at which a spare small-k slot deepens the top skill unless a
# sibling's lead fragment scores within the band of the top skill's lead.
_DEEPEN_BAND_DEFAULT = 0.0


def _deepen_band() -> float:
    """Deepen-band fraction from ``AGENTALLOY_DEEPEN_BAND`` (clamped to [0, 1]).

    Malformed/empty falls back to ``_DEEPEN_BAND_DEFAULT`` (0.0 == legacy).
    """
    try:
        return max(
            0.0,
            min(1.0, float(_os.environ.get("AGENTALLOY_DEEPEN_BAND", _DEEPEN_BAND_DEFAULT))),
        )
    except ValueError:
        return _DEEPEN_BAND_DEFAULT


# E5 — contract_tags as a soft domain filter (ships on; empty-fallback safe).
def _contract_tag_filter_enabled() -> bool:
    return _os.environ.get("AGENTALLOY_CONTRACT_TAG_FILTER", "on").strip().lower() != "off"


def _soft_tag_filter(
    ranked: list[ActiveFragment], contract_tags: list[str] | None
) -> list[ActiveFragment]:
    """Intersect the fused pool with fragments carrying >=1 contract tag.

    Falls back to the full pool when the intersection is empty (process-vocab
    contracts whose tags match no domain skill must not empty retrieval — the
    safety valve for ``legs="domain"`` contracts carrying only process tags). The
    pipeline already hydrates ``frag.domain_tags``. Additive to (not a replacement
    for) the BM25 steer in ``_resolve_bm25_query``.
    """
    if not contract_tags:
        return ranked
    want = {t.lower() for t in contract_tags}
    keep = [f for f in ranked if want & {t.lower() for t in f.domain_tags}]
    return keep if keep else ranked


# E7 — windowed process-class slot demotion (ships on; kill-switchable). The
# generic quality skills (test-driven-development, verification-before-completion,
# brainstorming, …) are skill_class="domain" with category_scope=[process]; they
# win free-text slots on virtually every task and evict the gold skill at small k
# (measured 2026-07-07: TDD on 18/18 domain tasks, gold evicted 3/18 at k=2).
# When the fused ranking shows evidence of an on-domain alternative — >=1
# non-process skill within the top-W skill window — every process-scope skill is
# moved to the back of the line: its fragments go to the tail of the candidate
# list (covers the RUNTIME_DIVERSITY_SELECTION=off top-k slice) and its skill_id
# is folded into skill_granular_select's FAR last-resort tier (covers staged
# selection, which otherwise round-robins tail siblings into first-pass slots).
# On generic tasks the window is all-process, so the transform is a strict no-op.
_PROCESS_SCOPE = "process"
_PROCESS_DEMOTION_WINDOW_FACTOR = 2  # W = factor * k distinct skills


def _process_demotion_enabled() -> bool:
    return _os.environ.get("AGENTALLOY_PROCESS_DEMOTION", "on").strip().lower() != "off"


def _process_demotion_window(k: int) -> int:
    """Window size W from ``AGENTALLOY_PROCESS_DEMOTION_WINDOW`` (default 2*k, min 1)."""
    raw = _os.environ.get("AGENTALLOY_PROCESS_DEMOTION_WINDOW", "")
    try:
        if raw.strip():
            return max(1, int(raw))
    except ValueError:
        pass
    return max(1, _PROCESS_DEMOTION_WINDOW_FACTOR * k)


def _is_process_scope(frag: ActiveFragment) -> bool:
    return frag.category_scope is not None and _PROCESS_SCOPE in frag.category_scope


def demote_process_skills(
    ranked: list[ActiveFragment], k: int
) -> tuple[list[ActiveFragment], frozenset[str]]:
    """Move process-scope skills to the back of the line when a domain skill competes.

    Deterministic pure transform. Fires only when >=1 non-process skill sits
    within the top-W distinct skills of ``ranked`` (first-fragment order,
    W = ``_process_demotion_window(k)``): then every process-scope fragment is
    stably moved to the tail and the demoted skill_ids are returned for
    ``skill_granular_select``'s FAR tier. Otherwise — generic-shaped pools, no
    process skills at all, or the kill switch — returns the input unchanged
    with an empty set.
    """
    if not ranked or not _process_demotion_enabled():
        return ranked, frozenset()

    skill_order = list(dict.fromkeys(f.skill_id for f in ranked))
    process_ids = {f.skill_id for f in ranked if _is_process_scope(f)}
    if not process_ids:
        return ranked, frozenset()

    window = skill_order[: _process_demotion_window(k)]
    if all(sid in process_ids for sid in window):
        return ranked, frozenset()

    preferred = [f for f in ranked if f.skill_id not in process_ids]
    demoted = [f for f in ranked if f.skill_id in process_ids]
    return preferred + demoted, frozenset(process_ids)


# E6 — phase/category pool gate (ships dormant; #14 activates). The benchmark
# packs share category="engineering" with React, so the old per-phase map can't
# separate them; the engine needs a product-category allowlist that excludes a
# reserved "benchmark" category (#14 assigns it at the pack level). Off by
# default (phase-agnostic, today's behavior) until #14's re-categorization +
# re-embed lands — then AGENTALLOY_PHASE_GATE=on activates it.
_PRODUCT_CATEGORIES: tuple[str, ...] = (
    "engineering",
    "design",
    "tooling",
    "quality",
    "ops",
    "operational",
    "review",
)


def _pool_categories() -> list[str] | None:
    """Category allowlist for the candidate pool, or ``None`` for phase-agnostic.

    Returns the product-category allowlist only when ``AGENTALLOY_PHASE_GATE=on``;
    the reserved "benchmark" category (#14) is excluded by omission so
    benchmark-only packs never enter production retrieval. Unset → ``None`` →
    byte-for-byte identical to today (a true no-op until #14 + the flip).
    """
    if _os.environ.get("AGENTALLOY_PHASE_GATE", "off").strip().lower() == "on":
        return list(_PRODUCT_CATEGORIES)
    return None


@runtime_checkable
class RequiresEdgeSource(Protocol):
    """Anything that can resolve a skill's REQUIRES_COMPOSITIONAL out-edges.

    Satisfied by ``RuntimeCache`` (edges loaded at startup). A bare
    ``SkillStore`` does not implement this, so graph expansion is a no-op on
    the store-backed path — the production pipeline always holds a RuntimeCache.
    """

    def get_required_skill_ids(self, skill_id: str) -> list[str]: ...


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


# Order of preference for structural diversity during reshuffle.
_DIVERSITY_PRIORITY: tuple[str, ...] = ("setup", "execution", "verification")


@dataclass(frozen=True)
class RetrievalResult:
    candidates: list[ActiveFragment]
    eligible_count: int
    retrieval_ms: int
    # Relevance score per fragment_id in (0, 1], descending with final fused
    # (RRF + card-boost) rank — 1.0 = top-ranked. Reflects both retrieval legs,
    # not dense cosine alone (which collapsed BM25-only hits to 0.0).
    scores_by_id: dict[str, float] = field(default_factory=lambda: {})
    bm25_source: str = "rule-extracted"  # "rule-extracted" | "contract" | "union"
    # Skill IDs ordered by rank (first fragment appearance) — observability for Stage B.
    skills_ranked: list[str] = field(default_factory=lambda: [])
    # True only when a cross-encoder rerank actually reordered the pool (Stage A).
    reranked: bool = False
    # Stage B (LM fragment re-rank) outcome for this composition. "disabled"
    # whenever LM_ASSIST=off or the stage never ran; otherwise hit/timeout/error.
    lm_assist_outcome: str = "disabled"
    # Stage B selection detail, populated only on a HIT (empty otherwise):
    # the fragment ids kept (above threshold) vs scored-but-dropped, and the
    # per-fragment scores over the full scored pool (fragment_id -> score).
    lm_assist_kept_ids: list[str] = field(default_factory=lambda: list[str]())
    lm_assist_dropped_ids: list[str] = field(default_factory=lambda: list[str]())
    lm_assist_scores: dict[str, float] = field(default_factory=lambda: dict[str, float]())
    # True when the dense leg was skipped because the bounded query came back
    # empty (a noise-only first turn). compose maps this to a degraded trace.
    dense_leg_degraded: bool = False


class StoreFragmentSource:
    """Thin adapter so a raw ``SkillStore`` satisfies ``FragmentSource``."""

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


def _apply_card_boost(fused_ids: list[str], skill_of: dict[str, str]) -> list[str]:
    """Resolve Stage 0 card hits into skill ranking, then drop the cards.

    Card documents (``card::<skill_id>``) ride the same fused list as real
    fragments. A card must NEVER be assembled into ``/compose`` output, but a
    high-ranking card *should* lift its skill — Stage 0's whole point. Because
    ``skill_granular_select`` ranks skills by the position of their first
    fragment, we promote each skill's fragments to its card's position when the
    card ranks higher, then strip every card id.

    ``skill_of`` maps real fragment_id → skill_id (from the hydrated metadata),
    so the boost is keyed on exact skill identity — never a fragment-id prefix
    guess. No cards in the list (the default ``off``/``prefix`` corpus) →
    returned unchanged: a no-op on a card-free index, preserving today's order.
    """
    if not any(is_card_id(fid) for fid in fused_ids):
        return fused_ids

    # Best (lowest) fused position at which each skill's card appeared.
    card_pos: dict[str, int] = {}
    for pos, fid in enumerate(fused_ids):
        if is_card_id(fid):
            sid = skill_id_from_card_id(fid)
            card_pos.setdefault(sid, pos)

    real = [fid for fid in fused_ids if not is_card_id(fid)]

    # Stable re-sort: a fragment's effective rank is the better of its own
    # fused position and its skill's card position. Ties keep fused order.
    def effective_key(item: tuple[int, str]) -> tuple[int, int]:
        idx, fid = item
        sid = skill_of.get(fid)
        cpos = card_pos.get(sid, idx) if sid is not None else idx
        return (min(idx, cpos), idx)

    return [fid for _, fid in sorted(enumerate(real), key=effective_key)]


def _bm25_fallback_result(
    frag_src: FragmentSource,
    vector_store: FragmentStore,
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
    # Phase-agnostic retrieval: the candidate pool is no longer gated by
    # phase->category eligibility. An A/B (gold-hit 18/18 and audit topic 0.97
    # identical with the gate on vs off, reranker off both arms) showed the hard
    # category filter is performance-neutral on the current corpus — the embedder
    # + BM25 + RRF already rank in-domain skills above cross-domain noise. phase
    # still drives k and the RRF leg weights below; it just no longer hard-gates.
    pool_size = max(k * 2, 50)
    deprecated_ids = frag_src.get_deprecated_skill_ids()
    bm25_query, bm25_source = _resolve_bm25_query(task, contract_tags)
    bm25_hits = vector_store.search_bm25(
        bm25_query,
        categories=_pool_categories(),
        phases=None,
        deprecated_skill_ids=deprecated_ids,
        k=pool_size,
    )

    metadata = frag_src.get_active_fragments(
        skill_class="domain",
        categories=_pool_categories(),
        phases=None,
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

    # E5: contract_tags soft filter — narrow the pool to fragments carrying >=1
    # contract tag, empty-fallback safe. No-op when contract_tags is None or the
    # AGENTALLOY_CONTRACT_TAG_FILTER kill-switch is off.
    if _contract_tag_filter_enabled():
        ranked = _soft_tag_filter(ranked, contract_tags)

    # E7: same process-class demotion as the dense path — the fallback leaks too.
    ranked, demoted_skill_ids = demote_process_skills(ranked, k)

    eligible_count = len(ranked)
    diversity_off = _os.environ.get("RUNTIME_DIVERSITY_SELECTION", "on").lower() == "off"
    if raw_scores or diversity_off:
        selected = ranked[:k]
    else:
        selected, _ = skill_granular_select(
            ranked,
            k,
            scores_by_id=scores_by_id,
            deepen_band=_deepen_band(),
            demoted_skill_ids=demoted_skill_ids,
        )
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
    vector_store: FragmentStore,
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
    ``SkillStore`` (wrapped automatically via ``StoreFragmentSource``).
    ``vector_store`` is a ``FragmentStore`` (the Lance ``fragments`` dataset)
    populated via the reembed CLI.

    Stages:

    1. Check circuit breaker — if open, skip embedding and return BM25-only
    2. bound the task (``build_retrieval_query``), embed via ``safe_embed``; empty -> BM25-only
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

    # Bound the retrieval query ONCE: strip injected context (<system-reminder>
    # blocks, CLAUDE.md / environment dumps, fenced code) out of the first user
    # turn and head-cap to ~512 tokens. The raw task routinely overflowed the hard
    # 2048-token embed ceiling (the logged 6050-token 500 -> silent BM25 fallback),
    # and a focused query is a sharper dense vector besides. The same bounded query
    # also feeds the cross-encoder reranker and the LM scorer below; the phase-gate
    # path deliberately stays on full first-turn text (handled in classifier.py).
    query = build_retrieval_query(task)
    # An empty bounded query means the dense leg is skipped below; surface that as
    # a degraded trace (compose threads this through to dense_leg_degraded).
    dense_leg_degraded = not query

    # Phase-agnostic retrieval: the candidate pool is no longer gated by
    # phase->category eligibility. An A/B (gold-hit 18/18 and audit topic 0.97
    # identical with the gate on vs off, reranker off both arms) showed the hard
    # category filter is performance-neutral on the current corpus — the embedder
    # + BM25 + RRF already rank in-domain skills above cross-domain noise. phase
    # still drives k and the RRF leg weights below; it just no longer hard-gates.
    pool_size = max(k * 2, 50)
    deprecated_ids = frag_src.get_deprecated_skill_ids()

    # ------------------------------------------------------------------
    # Stage 2: Safe embedding with circuit-breaker integration
    # ------------------------------------------------------------------
    # An empty bounded query means the first turn was all injected noise with no
    # instruction left to embed; "" embeds to a constant, meaningless vector, so
    # skip the dense leg and let BM25 carry the request. dense_leg_degraded (set
    # above) marks the trace so this path is observable rather than silent.
    dense_hits: list[SimilarityHit] = []
    if query:
        try:
            embed_input = f"search_query: {query}"
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
        dense_hits = vector_store.search_similar(
            query_vec,
            categories=_pool_categories(),
            phases=None,
            deprecated_skill_ids=deprecated_ids,
            k=pool_size,
        )
    else:
        logger.warning(
            "retrieval query empty after bounding for task=%s phase=%s; BM25-only",
            task[:80],
            phase,
        )

    # BM25 query: contract tags take priority over rule-extracted keywords.
    # The paid LLM picked them deliberately; they're better keywords than
    # rule-extracted ones. Union mode enabled by AGENTALLOY_UNION_KEYWORDS=1.

    bm25_query, _bm25_source = _resolve_bm25_query(task, contract_tags)
    bm25_hits = vector_store.search_bm25(
        bm25_query,
        categories=_pool_categories(),
        phases=None,
        deprecated_skill_ids=deprecated_ids,
        k=pool_size,
    )
    bm25_ids = [h.fragment_id for h in bm25_hits]

    if not dense_hits and not bm25_hits:
        elapsed_ms = (time.perf_counter_ns() - start_ns) // 1_000_000
        return RetrievalResult(
            candidates=[],
            eligible_count=0,
            retrieval_ms=int(elapsed_ms),
            bm25_source=_bm25_source,
            dense_leg_degraded=dense_leg_degraded,
        )

    # Apply phase-specific RRF weights
    rrf_k, dense_weight, bm25_weight = _get_rrf_params(phase)
    fused_ids = _rrf_fuse(
        dense_hits, bm25_ids, k=rrf_k, dense_weight=dense_weight, bm25_weight=bm25_weight
    )

    # Hydrate ActiveFragment metadata from the source. Pull domain fragments
    # for the eligible categories; intersect with the fused ids.
    metadata = frag_src.get_active_fragments(
        skill_class="domain",
        categories=_pool_categories(),
        phases=None,
        domain_tags=domain_tags,
    )
    by_id = {f.fragment_id: f for f in metadata}

    # Stage 0 card exclusion + boost: a card hit lifts its skill's rank, then
    # every card id is stripped so cards can never be assembled. No-op on a
    # card-free (off/prefix) index — preserves today's order exactly.
    fused_ids = _apply_card_boost(fused_ids, {fid: f.skill_id for fid, f in by_id.items()})

    # Score each fragment by its position in the final (RRF + card-boost) order.
    # Using dense distance alone scored every BM25-only fragment 0.0 — so
    # lexical-only hits lost their fused rank in the per-skill dedup/sort in
    # orchestration/retrieve.py. A descending fused-rank score keeps every hit's
    # authoritative position and reproduces the card-boost reordering downstream.
    n_fused = len(fused_ids)

    ranked: list[ActiveFragment] = []
    scores_by_id: dict[str, float] = {}
    for i, fid in enumerate(fused_ids):
        frag = by_id.get(fid)
        if frag is None:
            continue
        ranked.append(frag)
        scores_by_id[fid] = 1.0 - (i / n_fused) if n_fused else 0.0

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

    # E5: contract_tags as a soft domain filter — narrow the hydrated pool to
    # fragments carrying >=1 contract tag (empty-fallback safe) so Stage A/B and
    # selection all operate on the narrowed pool. No-op when contract_tags is None
    # (direct /compose) or AGENTALLOY_CONTRACT_TAG_FILTER=off.
    if _contract_tag_filter_enabled():
        ranked = _soft_tag_filter(ranked, contract_tags)

    # E7: windowed process-class demotion — reorder covers the top-k slice paths
    # below; the demoted set folds into skill_granular_select's FAR tier.
    ranked, demoted_skill_ids = demote_process_skills(ranked, k)

    eligible_count = len(ranked)

    # raw_scores=True: return pre-diversity order (for /retrieve observability).
    # RUNTIME_DIVERSITY_SELECTION=off also short-circuits — used by eval harness.
    diversity_off = _os.environ.get("RUNTIME_DIVERSITY_SELECTION", "on").lower() == "off"
    reranked = False
    lm_outcome = LMAssistOutcome.DISABLED
    lm_detail: _LMArbitrationDetail | None = None
    if raw_scores or diversity_off:
        selected = ranked[:k]
        skills_ranked: list[str] = []
    else:
        # Stage A: cross-encoder rerank of the top skills before selection.
        # Best-effort — a failure or disabled stage leaves ``ranked`` untouched.
        ranked, reranked = _maybe_rerank(ranked, query)

        # Graph expansion (RETRIEVAL_GRAPH_EXPAND=on) runs *before* Stage B so
        # the reranker scores graph-expanded candidates too. Required-skill
        # neighbors of the top ranked skills are spliced into ``ranked`` ahead
        # of arbitration; the same expansion is re-applied to the final
        # selection below to guarantee those fragments survive the k-cap as
        # additive trailing candidates (idempotent — the present-skill guard
        # makes the second pass a no-op for anything Stage B already kept). Off
        # by default and a strict no-op when off.
        graph_expand = _graph_expand_enabled() and isinstance(source, RequiresEdgeSource)
        if graph_expand and isinstance(source, RequiresEdgeSource):
            ranked_skill_order = list(dict.fromkeys(f.skill_id for f in ranked))
            ranked = _graph_expand(ranked, ranked_skill_order, by_id, source)

        # Stage B: LM fragment re-rank. When enabled and it returns a HIT, it has
        # FILTERED the scored head down to the survivors above keep_threshold (in
        # fusion order, NOT capped at k). Those survivors are routed through
        # skill_granular_select below, so the HIT path keeps the same depth+diversity
        # guarantees as the deterministic path — it is no longer "diversity off". On
        # disabled/timeout/error it returns (None, outcome) and the deterministic
        # path below runs byte-for-byte as if Stage B did not exist. The
        # deterministic path never sees the reranker.
        lm_selected, lm_outcome, lm_detail = _maybe_lm_arbitrate(ranked, query, k)
        logger.info(
            "stage-b verdict: outcome=%s kept=%d dropped=%d k=%d candidates=%d",
            lm_outcome.value,
            len(lm_detail.kept_ids) if lm_detail else 0,
            len(lm_detail.dropped_ids) if lm_detail else 0,
            k,
            len(ranked),
        )
        if lm_selected is not None:
            # §D: route Stage B survivors through the SAME diversity selection as
            # the deterministic path. scores_by_id = the reranker yes-probabilities
            # over the survivors (feeds the deepen-gate; inert at deepen_band=0.0).
            selected, skills_ranked = skill_granular_select(
                lm_selected,
                k,
                scores_by_id=(lm_detail.scores if lm_detail else None),
                deepen_band=_deepen_band(),
                demoted_skill_ids=demoted_skill_ids,
            )
        else:
            selected, skills_ranked = skill_granular_select(
                ranked,
                k,
                scores_by_id=scores_by_id,
                deepen_band=_deepen_band(),
                demoted_skill_ids=demoted_skill_ids,
            )

        # Additive tail: re-append graph neighbors dropped by the k-cap so they
        # surface as trailing candidates without displacing the selection.
        if graph_expand and isinstance(source, RequiresEdgeSource):
            selected = _graph_expand(selected, skills_ranked, by_id, source)

    elapsed_ms = (time.perf_counter_ns() - start_ns) // 1_000_000
    return RetrievalResult(
        candidates=selected,
        eligible_count=eligible_count,
        retrieval_ms=int(elapsed_ms),
        scores_by_id=scores_by_id,
        bm25_source=_bm25_source,
        skills_ranked=skills_ranked,
        reranked=reranked,
        lm_assist_outcome=lm_outcome.value,
        lm_assist_kept_ids=list(lm_detail.kept_ids) if lm_detail else [],
        lm_assist_dropped_ids=list(lm_detail.dropped_ids) if lm_detail else [],
        lm_assist_scores=dict(lm_detail.scores) if lm_detail else {},
        dense_leg_degraded=dense_leg_degraded,
    )


def _graph_expand(
    selected: list[ActiveFragment],
    skills_ranked: list[str],
    pool_by_id: dict[str, ActiveFragment],
    edge_source: RequiresEdgeSource,
) -> list[ActiveFragment]:
    """Append REQUIRES_COMPOSITIONAL neighbors' top fragments as trailing candidates.

    For each of the top ``_GRAPH_EXPAND_TOP_SKILLS`` ranked skills, pull its
    one-hop ``requires`` targets and append each target's best-ranked fragment
    (from the already-fused metadata pool) to the tail — but only if that skill
    is not already represented in ``selected``. Existing candidates are never
    reordered or dropped; at most ``_GRAPH_EXPAND_MAX_FRAGMENTS`` are added.

    ``related`` edges are deliberately ignored in v1 (too noisy). A skill whose
    target has no fragment in the pool, or that is already present, is a no-op.
    """
    if not selected or not skills_ranked:
        return selected

    present_skills = {f.skill_id for f in selected}
    present_frag_ids = {f.fragment_id for f in selected}

    # Best fragment per skill in the fused pool = first occurrence (pool_by_id is
    # built from the fused order, so dict iteration preserves rank).
    best_frag_by_skill: dict[str, ActiveFragment] = {}
    for frag in pool_by_id.values():
        best_frag_by_skill.setdefault(frag.skill_id, frag)

    appended: list[ActiveFragment] = []
    for source_skill in skills_ranked[:_GRAPH_EXPAND_TOP_SKILLS]:
        if len(appended) >= _GRAPH_EXPAND_MAX_FRAGMENTS:
            break
        for target_id in edge_source.get_required_skill_ids(source_skill):
            if len(appended) >= _GRAPH_EXPAND_MAX_FRAGMENTS:
                break
            if target_id in present_skills:
                continue  # never duplicate a skill already in the result
            frag = best_frag_by_skill.get(target_id)
            if frag is None or frag.fragment_id in present_frag_ids:
                continue
            appended.append(frag)
            present_skills.add(target_id)
            present_frag_ids.add(frag.fragment_id)

    if appended:
        logger.debug(
            "graph expansion appended %d required-skill fragment(s): %s",
            len(appended),
            [f.fragment_id for f in appended],
        )
    return selected + appended


def _maybe_rerank(ranked: list[ActiveFragment], query: str) -> tuple[list[ActiveFragment], bool]:
    """Reorder ``ranked`` by cross-encoder relevance over the top skills.

    Groups fragments by skill (rank order; dict insertion order gives this for
    free, same trick as ``skill_granular_select``), takes the top
    ``RUNTIME_RERANK_MAX_PAIRS`` skills, scores each skill's best fragment
    against ``query``, and reorders those skills by score descending (stable for
    ties). Skills beyond the cap keep their original relative order after the
    reranked ones. Within-skill fragment order is preserved throughout.

    Returns ``(ranked, reranked)`` where ``reranked`` is True only when the
    scorer succeeded and produced a reordering input. Any failure, disabled
    stage, or trivial pool returns the input list unchanged with False — this
    function never raises.
    """
    if not query:
        # Nothing instruction-bearing survived query bounding; skip reranking.
        return ranked, False
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
        scores = reranker.score(query, passages)
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


@dataclass(frozen=True)
class _LMArbitrationDetail:
    """Stage B selection bookkeeping for telemetry — ``kept_ids`` = the survivors
    that cleared the keep_threshold filter (NOT necessarily the final injected set,
    which is the downstream skill_granular_select result over the survivors),
    ``dropped_ids`` = scored-but-below-threshold, and ``scores`` = the per-fragment
    yes-probabilities over the full scored pool (fragment_id -> score)."""

    kept_ids: list[str]
    dropped_ids: list[str]
    scores: dict[str, float]


def _maybe_lm_arbitrate(
    ranked: list[ActiveFragment], query: str, k: int
) -> tuple[list[ActiveFragment] | None, LMAssistOutcome, _LMArbitrationDetail | None]:
    """Stage B — LM fragment re-rank (relevance FILTER) over the top fused fragments.

    Scores up to ``max_candidates()`` top fused fragments against ``query`` and
    returns the *survivors* — those whose yes-probability clears the configured
    keep_threshold, in fusion order, **uncapped**. The ``k`` cap is NOT applied
    here: the caller routes the survivors through ``skill_granular_select`` (with
    the reranker scores), so the HIT path gets the same depth+diversity selection
    as the deterministic path. An empty survivor set is a *valid* high-confidence
    result meaning "inject nothing" and is returned as ``[]`` (not None).

    At the inert default keep_threshold (0.0, gated-off) EVERY scored fragment
    survives (the keep test is ``score >= threshold`` and reranker probabilities are
    in [0, 1]), so the value is the restored selection routing, not the filter. 0.0
    (not 0.05) is the truly-inert default: a task whose candidates all score 0.0 must
    not be emptied before the P(yes) measurement gate sets a real prod threshold.

    ``k`` is accepted for call-site symmetry but unused (the cap lives downstream).

    Returns ``(survivors, outcome, detail)``:

    * ``(list, HIT, detail)`` — Stage B ran and filtered (survivors possibly empty).
    * ``(None, DISABLED|TIMEOUT|ERROR, None)`` — Stage B did not produce a result;
      the caller must fall through to deterministic selection. This is the
      contractual fail-open floor: ANY failure routes here and the
      deterministic path runs as if Stage B never existed.

    Never raises — the scorer swallows its own errors and this function guards
    the call site too.
    """
    if not query:
        # Nothing instruction-bearing survived query bounding; defer to the
        # deterministic path.
        return None, LMAssistOutcome.DISABLED, None
    scorer = build_scorer_from_env()
    if scorer is None:
        return None, LMAssistOutcome.DISABLED, None
    if not ranked:
        # Nothing to arbitrate; let the (also-empty) deterministic path run.
        return None, LMAssistOutcome.DISABLED, None

    head = ranked[: max_candidates()]
    # Document = skill identity + fragment body, same framing as Stage A: bare
    # fragment prose often omits the skill's topic, which short tasks carry.
    documents = [f"{f.skill_id.replace('-', ' ')}: {f.content}" for f in head]

    try:
        result = scorer.score(query, documents)
    except Exception:  # pyright: ignore[reportBroadExceptionCaught]
        logger.warning("lm-assist Stage B raised at call site; using deterministic selection")
        return None, LMAssistOutcome.ERROR, None

    if result.outcome is not LMAssistOutcome.HIT or len(result.scores) != len(head):
        # Disabled/timeout/error, or a length mismatch — fail open.
        outcome = (
            result.outcome if result.outcome is not LMAssistOutcome.HIT else LMAssistOutcome.ERROR
        )
        return None, outcome, None

    threshold = load_config().keep_threshold
    # FILTER (don't cap): keep every scored-head fragment at/above the threshold,
    # in fusion order. The ``k`` cap is applied downstream by skill_granular_select.
    survivors = [
        frag for frag, score in zip(head, result.scores, strict=True) if score >= threshold
    ]
    # Telemetry detail over the full scored pool: scores keyed by fragment id,
    # kept = the survivors (cleared the filter — NOT the final injected set, which
    # is the post-skill_granular_select result), dropped = scored-but-below-threshold.
    scores = {frag.fragment_id: score for frag, score in zip(head, result.scores, strict=True)}
    kept_ids = [frag.fragment_id for frag in survivors]
    kept_set = set(kept_ids)
    dropped_ids = [frag.fragment_id for frag in head if frag.fragment_id not in kept_set]
    detail = _LMArbitrationDetail(kept_ids=kept_ids, dropped_ids=dropped_ids, scores=scores)
    return survivors, LMAssistOutcome.HIT, detail


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
    ranked: list[ActiveFragment],
    k: int,
    *,
    scores_by_id: dict[str, float] | None = None,
    deepen_band: float = 0.0,
    demoted_skill_ids: frozenset[str] | set[str] | None = None,
) -> tuple[list[ActiveFragment], list[str]]:
    """Depth-guaranteed round-robin selection across skills.

    Groups fragments by skill_id in rank order (dict insertion order gives this
    for free — each skill's rank = its first fragment's position in ``ranked``).

    Stage 1 — top-skill depth guarantee: the top-ranked skill is granted up to
    ``k // 2`` slots before any other skill is considered. Strict 1-per-skill
    round-robin starved the best-matching skill of its convention-bearing
    fragments: with k=4 the gold skill contributed a single (often overview)
    fragment while three sibling skills filled the rest, so the specific
    headers/durations/API names the skill exists to teach never reached the
    prompt (measured: 2026-06 campaign, composed-vs-oracle gap concentrated in
    exactly those criteria).

    Stage 2 — round-robin over the NEAR sibling skills (including the top skill,
    if it has fragments left) fills the remaining slots, preventing sibling-skill
    cannibalization exactly as before.

    Stage 4 (deepen-gate) — ``scores_by_id`` + ``deepen_band`` partition the
    non-top siblings into NEAR (lead fragment scores within ``deepen_band`` of the
    top skill's lead) and FAR (below the band). Stages 2/3 spend the budget on
    NEAR siblings then the top skill's leftovers; only if the budget is still
    unfilled does Stage 4 admit FAR siblings as a last resort, so k is always
    filled. ``deepen_band=0.0`` (the shipped default) collapses the threshold to
    0.0 → FAR is empty, NEAR is all siblings → byte-for-byte identical to the
    legacy breadth-first behavior.

    Within each skill's queue, prefer a ``fragment_type`` from
    ``_DIVERSITY_PRIORITY`` not yet represented in the globally selected set
    (same logic as ``diversity_select``); otherwise take the skill's next
    highest-ranked fragment.

    Args:
        scores_by_id: per-fragment fused-rank scores (1 - i/n) for the lead-score
            lookup; ``None`` (or omitted) disables the gate.
        deepen_band: fraction of the top skill's lead score a sibling must clear
            to count as NEAR; 0.0 == legacy (gate inert).
        demoted_skill_ids: skills (E7 process-class demotion) forced into the
            FAR last-resort tier regardless of band — they win slots only after
            NEAR siblings are drained and the top skill is fully deepened.
            ``None``/empty == legacy.

    Returns:
        (selected, skills_ranked) where ``skills_ranked`` is the ordered list
        of all distinct skill_ids in rank order (not just the winning ones).

    Degenerate cases:
    - Empty input → ([], []).
    - k <= 1 → depth stage is a no-op (k // 2 == 0); pure round-robin.
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

    # Stage 1: depth guarantee for the top-ranked skill (type-diverse within it).
    depth = k // 2
    top_queue = queues[skills_ranked[0]]
    for _ in range(depth):
        if not top_queue:
            break
        chosen_index = _pick_diverse_index(top_queue, selected_types)
        frag = top_queue.pop(chosen_index)
        selected.append(frag)
        selected_types.add(frag.fragment_type)

    # Deepen-gate partition (E4). Partition the non-top "sibling" skills into
    # NEAR (lead fragment scores within ``deepen_band`` of the top skill's lead)
    # and FAR (below the band), using the ORIGINAL immutable ``skill_queues`` for
    # the lead-score lookup. At deepen_band=0.0 the threshold collapses to 0.0 →
    # NEAR == all siblings, FAR == [] → byte-for-byte identical to legacy.
    top_lead = (scores_by_id or {}).get(skill_queues[skills_ranked[0]][0].fragment_id, 0.0)
    threshold = deepen_band * top_lead if (scores_by_id and deepen_band > 0.0) else 0.0

    def _lead(sid: str) -> float:
        return (scores_by_id or {}).get(skill_queues[sid][0].fragment_id, 0.0)

    siblings = skills_ranked[1:] if depth and len(skills_ranked) > 1 else skills_ranked
    near = [s for s in siblings if threshold == 0.0 or _lead(s) >= threshold]
    far = [s for s in siblings if threshold > 0.0 and _lead(s) < threshold]

    # E7: demoted skills are FAR by fiat — last-resort backfill only. The demoted
    # set never contains the top skill when it comes from demote_process_skills
    # (the reorder puts a non-process skill at the head whenever the set is
    # non-empty), so the depth guarantee is unaffected.
    if demoted_skill_ids:
        far = (
            [s for s in far if s not in demoted_skill_ids]
            + [s for s in near if s in demoted_skill_ids]
            + [s for s in far if s in demoted_skill_ids]
        )
        near = [s for s in near if s not in demoted_skill_ids]

    # Stage 2: round-robin the remaining slots over the NEAR sibling skills — the
    # top skill already holds its depth slots, so the rest of the budget buys
    # breadth among siblings close enough to the top skill to be worth surfacing.
    # The top skill re-enters only in stage 3, when every near queue is exhausted.
    # When no depth slot was taken (k <= 1) the top skill keeps its place at the
    # head of the rotation (it is included in ``siblings``/``near``).
    while len(selected) < k:
        made_progress = False
        for sid in near:
            if len(selected) >= k:
                break
            queue = queues[sid]
            if not queue:
                continue
            chosen_index = _pick_diverse_index(queue, selected_types)
            frag = queue.pop(chosen_index)
            selected.append(frag)
            selected_types.add(frag.fragment_type)
            made_progress = True
        # Near queues exhausted before k fragments were gathered.
        if not made_progress:
            break

    # Stage 3: every near skill is drained — spend any remaining budget on the
    # top skill's leftover fragments (deepen the best match).
    top_queue = queues[skills_ranked[0]]
    while len(selected) < k and top_queue:
        chosen_index = _pick_diverse_index(top_queue, selected_types)
        frag = top_queue.pop(chosen_index)
        selected.append(frag)
        selected_types.add(frag.fragment_type)

    # Stage 4 (deepen-gate fallback): only when the gate fired (FAR non-empty) and
    # the budget is still unfilled after deepening the top skill, round-robin the
    # FAR (below-band) siblings so k is always filled. Inert when far == [] (the
    # deepen_band=0.0 default), preserving legacy behavior exactly.
    while len(selected) < k:
        made_progress = False
        for sid in far:
            if len(selected) >= k:
                break
            queue = queues[sid]
            if not queue:
                continue
            chosen_index = _pick_diverse_index(queue, selected_types)
            frag = queue.pop(chosen_index)
            selected.append(frag)
            selected_types.add(frag.fragment_type)
            made_progress = True
        if not made_progress:
            break

    return selected, skills_ranked


def _pick_diverse_index(queue: list[ActiveFragment], selected_types: set[str]) -> int:
    """Index of the queue's best fragment under the diversity preference.

    Prefer the highest-ranked fragment whose ``fragment_type`` is a
    ``_DIVERSITY_PRIORITY`` type not yet present in the globally selected set;
    fall back to the queue head.
    """
    for ptype in _DIVERSITY_PRIORITY:
        if ptype in selected_types:
            continue
        for i, frag in enumerate(queue):
            if frag.fragment_type == ptype:
                return i
    return 0
