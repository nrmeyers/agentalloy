"""Semantic predicate evaluator using cosine similarity against reference phrase sets.

Replaces the chat-model classifier (Phase 3) with embed-based similarity scoring
using the same embed server already running for retrieval. No new server or model
required.

Phase 7 update: raw query text for embed inputs (no instruct prefix — prefix
adds ~50 tokens of noise that dilutes cosine similarity for short text-to-text
classification), expanded reference phrase sets (12+ per intent), recalibrated
similarity threshold.

Reranker backend (default-on; ``SIGNAL_INTENT_BACKEND=cosine`` opts out): the
named-intent predicates score the utterance against each intent's task
description with the qwen3-reranker cross-encoder (the Stage-B ``FragmentScorer``,
reused via a custom instruct) instead of cosine similarity. On the labeled
intent benchmark this lifts per-intent macro-F1 from 0.242 (cosine @ 0.75) to
0.687, which is why it ships as the default. The cross-encoder over-fires on
negated cue words ("not done", "don't approve"), so a deterministic negation
guard vetoes those before scoring. Cosine remains the fail-open floor: an
unreachable / failed reranker (e.g. no qwen3-reranker server at the configured
URL), an explicit ``SIGNAL_INTENT_BACKEND=cosine``, or an intent with no task
description all fall through to cosine byte-for-byte — so the default is safe
even where the reranker server is not running.

Four semantic predicates:
  user_intent_matches      — prompt similarity against named intent references
  agent_intent_matches     — agent-tool use similarity against named intent references
                             (falls back to prompt if no tool use event)
  artifact_completeness    — soft advisory only; always returns UNKNOWN (gate handling in gates.py)
  prompt_topic_matches     — prompt similarity against topic phrases
"""

from __future__ import annotations

import contextlib
import logging
import math
import os
import re
import threading
from collections.abc import Callable
from typing import Any

from agentalloy.embed_provider import EmbedClient
from agentalloy.signals.predicates import PredicateContext, PredicateResult
from agentalloy.signals.prefilter import PreFilterMatch, check_prefilter

_log = logging.getLogger(__name__)

# Reference phrases per named intent. Extended per Phase 7 (12+ per intent).
# Completion phrases avoid "looks good" / "good to go" to reduce overlap with approval.
_INTENT_REFERENCES: dict[str, list[str]] = {
    "completion": [
        "done with spec",
        "ready to move on",
        "spec is complete",
        "finished",
        "that covers it",
        "we're done here",
        "all set",
        "wrap it up",
        "I think that covers it",
        "nothing more to add",
        "moving on",
    ],
    "approval": [
        "looks good",
        "approve",
        "ship it",
        "lgtm",
        "approved",
        "+1",
        "yes do that",
        "go ahead",
        "yep that works",
        "perfect",
        "exactly right",
        "merge it",
    ],
    "redirection": [
        "let's change direction",
        "scratch that",
        "new approach",
        "start over",
        "different direction",
        "this isn't working",
        "let's try something else",
        "back up",
        "actually no",
        "rethink this",
        "go a different way",
        "abandon this approach",
    ],
}

# Per-intent task descriptions for the Qwen instruct prefix.
# Mirrors retrieval/domain.py:217 conventions.
_INTENT_TASK_DESCRIPTIONS: dict[str, str] = {
    "completion": "Decide whether the user is signaling that they consider the current artifact or step complete.",
    "approval": "Decide whether the user is approving recent work or output.",
    "redirection": "Decide whether the user is asking to change direction or abandon the current approach.",
}

# Validate that every intent has a matching task description at startup.
if set(_INTENT_TASK_DESCRIPTIONS.keys()) != set(_INTENT_REFERENCES.keys()):
    raise ValueError(
        f"_INTENT_TASK_DESCRIPTIONS keys {set(_INTENT_TASK_DESCRIPTIONS)} != "
        f"_INTENT_REFERENCES keys {set(_INTENT_REFERENCES)}"
    )

# Recalibrated per Phase 7 calibration script.
# nomic-calibrated (in-sample optimum from intent_bench sweep)
_SIMILARITY_THRESHOLD = 0.56
_MAX_INPUT_CHARS = 2000

# ---------------------------------------------------------------------------
# Reranker backend (default-on; SIGNAL_INTENT_BACKEND=cosine opts out). See module docstring.
# ---------------------------------------------------------------------------

# Intent-framed instruct for the cross-encoder. The Stage-B default instruct is
# about skill-fragment relevance and scores ~0 for these utterances; this frames
# the yes/no question as intent classification.
_INTENT_INSTRUCT = (
    "Judge whether the user message expresses the intent described. "
    "Answer yes only if the message clearly signals that intent."
)

# Operating threshold for the reranker yes-probability. Calibrated per-intent
# (each gate queries one intent — no argmax) against the 2026-06-12 labeled
# benchmark: macro-F1 peaks at ~0.45 (0.44–0.46 plateau). Env-overridable.
_DEFAULT_RERANK_THRESHOLD = 0.45

# AgentAlloy's own reranker (llama-server) listens on 47952 — see
# install/presets/*.yaml and install/subcommands/start_rerank_server.py. The
# old default (60001) pointed at an unrelated local service; when nothing was
# listening there the intent scorer silently fell through to the cosine floor.
_DEFAULT_RERANK_URL = "http://127.0.0.1:47952"
_DEFAULT_RERANK_MODEL = "Qwen3-Reranker-0.6B-Q8_0.gguf"
_DEFAULT_RERANK_TIMEOUT_MS = 300

# Negation / cancellation cues that contradict an apparent finished/approved/
# redirect reading. The cross-encoder fires on the surface cue word ("done",
# "approve", "scratch that") even when it is negated; this deterministic veto
# recovers the negation slice (benchmark 2026-06-12: reranker negation-slice
# accuracy 0.46 → 1.00 with the guard). Conservative by design — a false veto
# only withholds a transition for one turn (the user re-signals), whereas a
# false fire advances the phase incorrectly.
_NEGATION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bnot\b",  # "this is NOT done", "I'm NOT approving"
        r"n['’]t\b",  # don't / can't / wouldn't / isn't
        r"\bfar from\b",  # "far from finished"
        r"\bnot quite\b",  # "almost done but not quite"
        r"\bno need\b",  # "no need to change direction"
        r"\bnever mind\b",  # "actually never mind"
        r"\bhold off\b",  # "hold off on merging"
        r"\bmissing\b",  # "still missing pieces"
    )
)


def _has_negation(text: str) -> bool:
    """True if the utterance carries a negation/cancellation cue (see above)."""
    return any(p.search(text) for p in _NEGATION_PATTERNS)


def _intent_backend() -> str:
    """Selected named-intent backend: ``"reranker"`` (default) or ``"cosine"``."""
    return os.environ.get("SIGNAL_INTENT_BACKEND", "reranker").strip().lower() or "reranker"


def _rerank_threshold() -> float:
    raw = os.environ.get("SIGNAL_INTENT_RERANK_THRESHOLD")
    if raw is None or not raw.strip():
        return _DEFAULT_RERANK_THRESHOLD
    try:
        return float(raw)
    except ValueError:
        _log.warning("invalid SIGNAL_INTENT_RERANK_THRESHOLD=%r; using default", raw)
        return _DEFAULT_RERANK_THRESHOLD


# Process-wide scorer cache (mirrors lm_assist / rerank factory pattern).
_scorer_lock = threading.Lock()
_scorer_cache: Any = None
_scorer_built = False


def _build_intent_scorer_from_env() -> Any:
    """Construct the reranker scorer, or None when the backend is off / mis-built.

    Never raises — a build failure logs one warning and returns None so the
    caller falls through to the cosine floor.
    """
    if _intent_backend() != "reranker":
        return None
    from agentalloy.retrieval.lm_assist import (
        FragmentScorer,
        LMAssistConfig,
        LMAssistMode,
    )

    config = LMAssistConfig(
        mode=LMAssistMode.ARBITRATE,
        url=os.environ.get("SIGNAL_INTENT_RERANK_URL", _DEFAULT_RERANK_URL).strip().rstrip("/")
        or _DEFAULT_RERANK_URL,
        timeout_ms=_env_int("SIGNAL_INTENT_RERANK_TIMEOUT_MS", _DEFAULT_RERANK_TIMEOUT_MS),
        keep_threshold=0.0,  # unused: the classifier thresholds the raw score itself
        model=os.environ.get("SIGNAL_INTENT_RERANK_MODEL", _DEFAULT_RERANK_MODEL).strip()
        or _DEFAULT_RERANK_MODEL,
        instruct=_INTENT_INSTRUCT,
    )
    try:
        return FragmentScorer(config)
    except Exception as exc:  # pyright: ignore[reportBroadExceptionCaught]
        _log.warning("signal-layer reranker disabled — scorer build failed: %s", exc)
        return None


def build_intent_scorer_from_env() -> Any:
    """Return the process-wide reranker scorer, building it once. None = cosine."""
    global _scorer_cache, _scorer_built
    with _scorer_lock:
        if not _scorer_built:
            _scorer_cache = _build_intent_scorer_from_env()
            _scorer_built = True
        return _scorer_cache


def reset_intent_scorer_cache() -> None:
    """Drop the cached scorer so the next call rebuilds from env (tests)."""
    global _scorer_cache, _scorer_built
    with _scorer_lock:
        scorer = _scorer_cache
        _scorer_cache = None
        _scorer_built = False
    if scorer is not None:
        with contextlib.suppress(Exception):
            scorer.close()


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        _log.warning("invalid %s=%r; using default %d", name, raw, default)
        return default


def _intent_rerank(
    text: str,
    intent: str,
    scorer: Any,
    threshold: float,
) -> PredicateResult | None:
    """Score one utterance against one intent's task description.

    Returns MET/NOT_MET on a clean verdict, or ``None`` to signal "fall back to
    the cosine floor" (unknown intent, or any scorer failure). The negation
    guard short-circuits to NOT_MET before scoring.
    """
    from agentalloy.retrieval.lm_assist import LMAssistOutcome

    desc = _INTENT_TASK_DESCRIPTIONS.get(intent)
    if desc is None:
        return None  # no task description → cosine floor
    if _has_negation(text):
        _log.debug("negation guard vetoed intent=%r for %r", intent, text[:80])
        return PredicateResult.NOT_MET
    result = scorer.score(text[:_MAX_INPUT_CHARS], [desc])
    if result.outcome is not LMAssistOutcome.HIT or len(result.scores) != 1:
        return None  # disabled / timeout / error → cosine floor
    score = result.scores[0]
    _log.debug("rerank intent=%r score=%.3f threshold=%.3f", intent, score, threshold)
    return PredicateResult.MET if score >= threshold else PredicateResult.NOT_MET


def _classify_intent(
    text: str,
    intent: str,
    lm_client: EmbedClient,
    model: str,
    ctx: PredicateContext | None = None,
) -> PredicateResult:
    """Named-intent decision via the selected backend, cosine as the floor.

    ``ctx`` (when provided) receives an embed-failure flag if the cosine floor's
    embed call errors — the reranker leg never embeds, so only the floor records.
    """
    scorer = build_intent_scorer_from_env()
    if scorer is not None:
        verdict = _intent_rerank(text, intent, scorer, _rerank_threshold())
        if verdict is not None:
            return verdict
    return _intent_similarity(text, intent, lm_client, model, ctx=ctx)


# Forward-transition intents: the user/agent signaling the current phase's work
# is done ("completion") or approving recent output so we can advance ("approval").
# "redirection" is deliberately excluded — it means abandon/change course, not
# advance to the next phase.
_TRANSITION_INTENTS: tuple[str, ...] = ("completion", "approval")


def check_transition_trigger(
    signal_keywords: list[str],
    gate_spec: Any,
    ctx: PredicateContext,
    lm_client: EmbedClient | None,
    model: str | None = None,
) -> PreFilterMatch | None:
    """Decide whether to evaluate phase-exit gates this turn (reranker-primary).

    The semantic intent layer (reranker-backed, cosine floor — see
    ``_classify_intent``) scores the prompt against the forward-transition
    intents FIRST, so natural-language phrasing ("looks right, now the design")
    engages the gates even when it matches none of the rigid ``signal_keywords``.

    The deterministic :func:`check_prefilter` (keyword / artifact-event /
    tool-use) remains the fallback floor: it still fires on explicit keywords and
    file events, and it is the sole path when no embed/reranker client is
    available. The semantic layer can only *add* positives — it never suppresses
    a deterministic signal.
    """
    # Manual override stays authoritative (mirrors check_prefilter).
    if os.environ.get("AGENTALLOY_FORCE_CHECK") == "1":
        return PreFilterMatch(name="manual", detail="AGENTALLOY_FORCE_CHECK=1")

    # Primary: semantic intent on the user's prompt.
    text = (ctx.recent_prompt_text or "").strip()
    if text and lm_client is not None:
        if model is None:
            try:
                from agentalloy.config import get_settings

                model = get_settings().runtime_embedding_model
            except Exception:
                model = None
        if model:
            for intent in _TRANSITION_INTENTS:
                if _classify_intent(text, intent, lm_client, model, ctx=ctx) is PredicateResult.MET:
                    return PreFilterMatch(name="intent", detail=f"intent={intent}")

    # Fallback floor: deterministic keyword / artifact-event / tool-use match.
    return check_prefilter(signal_keywords, gate_spec, ctx)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _cosine_with_qnorm(a: list[float], norm_a: float, b: list[float]) -> float:
    """``_cosine(a, b)`` with ``a``'s norm precomputed (loop-invariant hoist).

    Bit-identical to ``_cosine``: same dot / norm_b float ops and the same
    zero-norm short-circuit. Lets the similarity loops compute the query norm
    once instead of once per reference phrase.
    """
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _intent_similarity(
    text: str,
    intent: str,
    lm_client: EmbedClient,
    model: str,
    threshold: float = _SIMILARITY_THRESHOLD,
    ctx: PredicateContext | None = None,
) -> PredicateResult:
    refs = _INTENT_REFERENCES.get(intent)
    if not refs:
        _log.debug("unknown intent %r — returning UNKNOWN", intent)
        return PredicateResult.UNKNOWN
    query = text[:_MAX_INPUT_CHARS]
    try:
        vecs = lm_client.embed(
            model=model,
            texts=[f"search_query: {query}"] + [f"search_query: {r}" for r in refs],
        )
    except Exception as exc:
        _log.warning("phase-gate embed failed; gate -> UNKNOWN (transition may not fire): %s", exc)
        if ctx is not None:
            ctx.record_embed_failure()
        return PredicateResult.UNKNOWN
    query_vec = vecs[0]
    qn = math.sqrt(sum(x * x for x in query_vec))  # query norm is loop-invariant
    best = max(_cosine_with_qnorm(query_vec, qn, r) for r in vecs[1:])
    _log.debug("intent=%r best_similarity=%.3f threshold=%.3f", intent, best, threshold)
    return PredicateResult.MET if best >= threshold else PredicateResult.NOT_MET


def _topic_similarity(
    text: str,
    topics: list[str],
    lm_client: EmbedClient,
    model: str,
    threshold: float = _SIMILARITY_THRESHOLD,
    ctx: PredicateContext | None = None,
) -> PredicateResult:
    if not topics:
        return PredicateResult.UNKNOWN
    query = text[:_MAX_INPUT_CHARS]
    try:
        vecs = lm_client.embed(
            model=model,
            texts=[f"search_query: {query}"] + [f"search_query: {t}" for t in topics],
        )
    except Exception as exc:
        _log.warning("phase-gate embed failed; gate -> UNKNOWN (transition may not fire): %s", exc)
        if ctx is not None:
            ctx.record_embed_failure()
        return PredicateResult.UNKNOWN
    query_vec = vecs[0]
    qn = math.sqrt(sum(x * x for x in query_vec))  # query norm is loop-invariant
    best = max(_cosine_with_qnorm(query_vec, qn, r) for r in vecs[1:])
    _log.debug("topics=%r best_similarity=%.3f threshold=%.3f", topics, best, threshold)
    return PredicateResult.MET if best >= threshold else PredicateResult.NOT_MET


def eval_user_intent_matches(
    args: dict[str, Any],
    ctx: PredicateContext,
    lm_client: EmbedClient,
    model: str,
) -> PredicateResult:
    # recent_prompts arg is not supported; similarity runs against recent_prompt_text only.
    intent = args.get("intent", "")
    text = (ctx.recent_prompt_text or "").strip()
    if not text or not intent:
        return PredicateResult.UNKNOWN
    return _classify_intent(text, intent, lm_client, model, ctx=ctx)


def eval_agent_intent_matches(
    args: dict[str, Any],
    ctx: PredicateContext,
    lm_client: EmbedClient,
    model: str,
) -> PredicateResult:
    """Evaluate whether the agent's intent matches a named intent.

    Reads from recent_tool_use (the tool the agent is invoking) rather than
    recent_prompt_text (the user's prompt), since this predicate is about
    the agent's own intent, not the user's request.
    Falls back to recent_prompt_text if no tool use event is available.
    """
    intent = args.get("intent", "")
    if not intent:
        return PredicateResult.UNKNOWN

    # Prefer recent_tool_use (agent's action) over recent_prompt_text (user's request)
    if ctx.recent_tool_use is not None:
        tool_name = ctx.recent_tool_use.get("tool", "")
        text = tool_name.strip()
    else:
        text = (ctx.recent_prompt_text or "").strip()

    if not text:
        return PredicateResult.UNKNOWN
    return _classify_intent(text, intent, lm_client, model, ctx=ctx)


def eval_artifact_completeness(
    args: dict[str, Any],
    ctx: PredicateContext,
    lm_client: EmbedClient,
    model: str,
) -> PredicateResult:
    # Soft advisory only — gate handling (advisory emission) lives in gates.py.
    # This predicate always returns UNKNOWN so it never blocks a transition.
    return PredicateResult.UNKNOWN


def eval_prompt_topic_matches(
    args: dict[str, Any],
    ctx: PredicateContext,
    lm_client: EmbedClient,
    model: str,
) -> PredicateResult:
    topics = args.get("topics", [])
    text = (ctx.recent_prompt_text or "").strip()
    if not text or not topics:
        return PredicateResult.UNKNOWN
    return _topic_similarity(text, topics, lm_client, model, ctx=ctx)


SEMANTIC_PREDICATES: dict[
    str,
    Callable[
        [dict[str, Any], PredicateContext, EmbedClient, str],
        PredicateResult,
    ],
] = {
    "user_intent_matches": eval_user_intent_matches,
    "agent_intent_matches": eval_agent_intent_matches,
    "artifact_completeness": eval_artifact_completeness,
    "prompt_topic_matches": eval_prompt_topic_matches,
}
