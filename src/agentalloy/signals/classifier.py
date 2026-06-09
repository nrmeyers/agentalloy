"""Semantic predicate evaluator using cosine similarity against reference phrase sets.

Replaces the chat-model classifier (Phase 3) with embed-based similarity scoring
using the same embed server already running for retrieval. No new server or model
required.

Phase 7 update: raw query text for embed inputs (no instruct prefix — prefix
adds ~50 tokens of noise that dilutes cosine similarity for short text-to-text
classification), expanded reference phrase sets (12+ per intent), recalibrated
similarity threshold.

Four semantic predicates:
  user_intent_matches      — prompt similarity against named intent references
  agent_intent_matches     — agent-tool use similarity against named intent references
                             (falls back to prompt if no tool use event)
  artifact_completeness    — soft advisory only; always returns UNKNOWN (gate handling in gates.py)
  prompt_topic_matches     — prompt similarity against topic phrases
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from typing import Any

from agentalloy.embed_provider import EmbedClient
from agentalloy.signals.predicates import PredicateContext, PredicateResult

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
_SIMILARITY_THRESHOLD = 0.75
_MAX_INPUT_CHARS = 2000


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
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
) -> PredicateResult:
    refs = _INTENT_REFERENCES.get(intent)
    if not refs:
        _log.debug("unknown intent %r — returning UNKNOWN", intent)
        return PredicateResult.UNKNOWN
    query = text[:_MAX_INPUT_CHARS]
    try:
        vecs = lm_client.embed(model=model, texts=[query] + refs)
    except Exception as exc:
        _log.debug("embed call failed: %s", exc)
        return PredicateResult.UNKNOWN
    query_vec = vecs[0]
    best = max(_cosine(query_vec, r) for r in vecs[1:])
    _log.debug("intent=%r best_similarity=%.3f threshold=%.3f", intent, best, threshold)
    return PredicateResult.MET if best >= threshold else PredicateResult.NOT_MET


def _topic_similarity(
    text: str,
    topics: list[str],
    lm_client: EmbedClient,
    model: str,
    threshold: float = _SIMILARITY_THRESHOLD,
) -> PredicateResult:
    if not topics:
        return PredicateResult.UNKNOWN
    query = text[:_MAX_INPUT_CHARS]
    try:
        vecs = lm_client.embed(model=model, texts=[query] + topics)
    except Exception as exc:
        _log.debug("embed call failed: %s", exc)
        return PredicateResult.UNKNOWN
    query_vec = vecs[0]
    best = max(_cosine(query_vec, r) for r in vecs[1:])
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
    return _intent_similarity(text, intent, lm_client, model)


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
    return _intent_similarity(text, intent, lm_client, model)


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
    return _topic_similarity(text, topics, lm_client, model)


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
