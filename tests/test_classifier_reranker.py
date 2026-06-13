"""Tests for the flag-gated reranker intent backend in signals/classifier.py.

No model downloads, no live network: the FragmentScorer is driven through a
faked httpx transport (fixed yes/no logprobs → known score). The headline
guarantees:

* **default-on** — with ``SIGNAL_INTENT_BACKEND`` unset the reranker scorer is
  built (the shipped default); ``SIGNAL_INTENT_BACKEND=cosine`` opts out.
* **fail-open floor** — a None / failing scorer, an unreachable server, an
  unknown backend value, or an intent with no task description all fall through
  to cosine byte-for-byte.
* **negation guard** — negated cue words are vetoed to NOT_MET before scoring.

The suite-wide conftest pins ``SIGNAL_INTENT_BACKEND=cosine`` for hermeticity;
the autouse ``_clean_rerank_env`` fixture below deletes that pin so these tests
see the true default.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

import agentalloy.signals.classifier as classifier
from agentalloy.retrieval.lm_assist import FragmentScorer, LMAssistConfig, LMAssistMode
from agentalloy.signals.classifier import (
    _DEFAULT_RERANK_THRESHOLD,  # pyright: ignore[reportPrivateUsage]
    _classify_intent,  # pyright: ignore[reportPrivateUsage]
    _has_negation,  # pyright: ignore[reportPrivateUsage]
    _intent_rerank,  # pyright: ignore[reportPrivateUsage]
    build_intent_scorer_from_env,
    eval_user_intent_matches,
    reset_intent_scorer_cache,
)
from agentalloy.signals.predicates import PredicateContext, PredicateResult

_RERANK_ENV = (
    "SIGNAL_INTENT_BACKEND",
    "SIGNAL_INTENT_RERANK_URL",
    "SIGNAL_INTENT_RERANK_MODEL",
    "SIGNAL_INTENT_RERANK_TIMEOUT_MS",
    "SIGNAL_INTENT_RERANK_THRESHOLD",
)


@pytest.fixture(autouse=True)
def _clean_rerank_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _RERANK_ENV:
        monkeypatch.delenv(name, raising=False)
    reset_intent_scorer_cache()
    yield
    reset_intent_scorer_cache()


# --------------------------------------------------------------------------
# Faked-transport scorer
# --------------------------------------------------------------------------


def _completion(top: list[tuple[str, float]]) -> dict[str, object]:
    return {
        "choices": [
            {
                "logprobs": {
                    "content": [{"top_logprobs": [{"token": t, "logprob": lp} for t, lp in top]}]
                }
            }
        ]
    }


def _scorer(yes_logprob: float, no_logprob: float = -10.0) -> FragmentScorer:
    """A FragmentScorer whose every pair returns a fixed (yes, no) logprob."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion([("yes", yes_logprob), ("no", no_logprob)]))

    cfg = LMAssistConfig(
        mode=LMAssistMode.ARBITRATE,
        url="http://test",
        timeout_ms=300,
        keep_threshold=0.0,
        model="m",
        instruct=classifier._INTENT_INSTRUCT,  # pyright: ignore[reportPrivateUsage]
    )
    scorer = FragmentScorer(cfg)
    scorer._client = httpx.Client(  # pyright: ignore[reportPrivateUsage]
        transport=httpx.MockTransport(handler), base_url="http://test"
    )
    return scorer


# --------------------------------------------------------------------------
# negation guard
# --------------------------------------------------------------------------

# The 12 negation utterances the guard is expected to flag (the 13th in the
# benchmark — "I almost said scratch that but let's keep going" — has no lexical
# cue and is left to the threshold; see module docstring).
_NEGATION_HITS = [
    "this is NOT done yet, there are gaps in the spec",
    "we are far from finished here, lots missing",
    "I wouldn't call this complete, keep adding to it",
    "almost done but not quite, hold on",
    "is the spec complete or are we still missing pieces?",
    "don't approve this yet, I have concerns",
    "I'm NOT approving this in its current state",
    "hold off on merging, something feels off",
    "I can't sign off on this until the tests pass",
    "no need to change direction, this is working fine",
    "don't start over, just tweak the edge case",
    "I was going to switch approaches but actually never mind",
]

# Genuine intent signals that must NOT be vetoed (no false negation cue).
_NEGATION_MISSES = [
    "nothing more to add, we're done",  # "nothing" must not trip \bnot\b
    "looks good, ship it",
    "approved, go ahead and merge",
    "let's scratch that and start over",
    "this is complete, moving on",
    "no concerns, approve it",  # "no" alone must not veto
]


@pytest.mark.parametrize("text", _NEGATION_HITS)
def test_negation_guard_flags(text: str) -> None:
    assert _has_negation(text) is True


@pytest.mark.parametrize("text", _NEGATION_MISSES)
def test_negation_guard_does_not_overfire(text: str) -> None:
    assert _has_negation(text) is False


# --------------------------------------------------------------------------
# _intent_rerank
# --------------------------------------------------------------------------


def test_intent_rerank_met_above_threshold() -> None:
    scorer = _scorer(yes_logprob=0.0)  # yes≫no → ~1.0
    try:
        result = _intent_rerank("this is complete", "completion", scorer, _DEFAULT_RERANK_THRESHOLD)
    finally:
        scorer.close()
    assert result is PredicateResult.MET


def test_intent_rerank_not_met_below_threshold() -> None:
    scorer = _scorer(yes_logprob=-10.0, no_logprob=0.0)  # no≫yes → ~0.0
    try:
        result = _intent_rerank(
            "the weather is nice", "completion", scorer, _DEFAULT_RERANK_THRESHOLD
        )
    finally:
        scorer.close()
    assert result is PredicateResult.NOT_MET


def test_intent_rerank_negation_vetoes_without_scoring() -> None:
    scorer = MagicMock()  # high score would fire — guard must short-circuit first
    result = _intent_rerank("this is NOT done yet", "completion", scorer, _DEFAULT_RERANK_THRESHOLD)
    assert result is PredicateResult.NOT_MET
    scorer.score.assert_not_called()


def test_intent_rerank_unknown_intent_falls_back() -> None:
    scorer = MagicMock()
    result = _intent_rerank("text", "nonexistent_intent", scorer, _DEFAULT_RERANK_THRESHOLD)
    assert result is None  # None → caller uses cosine floor
    scorer.score.assert_not_called()


def test_intent_rerank_scorer_failure_falls_back() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    cfg = LMAssistConfig(
        mode=LMAssistMode.ARBITRATE,
        url="http://test",
        timeout_ms=300,
        keep_threshold=0.0,
        model="m",
    )
    scorer = FragmentScorer(cfg)
    scorer._client = httpx.Client(  # pyright: ignore[reportPrivateUsage]
        transport=httpx.MockTransport(boom), base_url="http://test"
    )
    try:
        result = _intent_rerank("this is complete", "completion", scorer, _DEFAULT_RERANK_THRESHOLD)
    finally:
        scorer.close()
    assert result is None  # ERROR outcome → cosine floor


# --------------------------------------------------------------------------
# backend selection / factory gating
# --------------------------------------------------------------------------


def test_backend_default_on_builds_scorer() -> None:
    """Env unset → the reranker backend is the default, so the scorer is built."""
    scorer = build_intent_scorer_from_env()
    try:
        assert scorer is not None
        assert isinstance(scorer, FragmentScorer)
    finally:
        reset_intent_scorer_cache()


def test_backend_cosine_optout_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit SIGNAL_INTENT_BACKEND=cosine opts out — no scorer built."""
    monkeypatch.setenv("SIGNAL_INTENT_BACKEND", "cosine")
    assert build_intent_scorer_from_env() is None


def test_backend_reranker_builds_scorer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIGNAL_INTENT_BACKEND", "reranker")
    scorer = build_intent_scorer_from_env()
    try:
        assert scorer is not None
        assert isinstance(scorer, FragmentScorer)
    finally:
        reset_intent_scorer_cache()


def test_backend_unknown_value_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unknown backend value fails safe to cosine (None), not to reranker."""
    monkeypatch.setenv("SIGNAL_INTENT_BACKEND", "magic")
    assert build_intent_scorer_from_env() is None


# --------------------------------------------------------------------------
# _classify_intent dispatch
# --------------------------------------------------------------------------


def test_classify_intent_cosine_optout_uses_cosine(monkeypatch: pytest.MonkeyPatch) -> None:
    """With SIGNAL_INTENT_BACKEND=cosine, _classify_intent calls cosine, not the reranker."""
    monkeypatch.setenv("SIGNAL_INTENT_BACKEND", "cosine")
    sentinel = PredicateResult.MET
    cosine_called: list[tuple[str, str]] = []

    def fake_cosine(text: str, intent: str, _client: object, _model: str) -> PredicateResult:
        cosine_called.append((text, intent))
        return sentinel

    monkeypatch.setattr(classifier, "_intent_similarity", fake_cosine)
    result = _classify_intent("looks good", "approval", MagicMock(), "embed-model")
    assert result is sentinel
    assert cosine_called == [("looks good", "approval")]


def test_classify_intent_reranker_short_circuits_cosine(monkeypatch: pytest.MonkeyPatch) -> None:
    """A clean reranker verdict must NOT fall through to cosine."""
    monkeypatch.setattr(
        classifier, "build_intent_scorer_from_env", lambda: _scorer(yes_logprob=0.0)
    )

    def fail_cosine(*_a: object, **_k: object) -> PredicateResult:
        raise AssertionError("cosine floor must not run when the reranker has a verdict")

    monkeypatch.setattr(classifier, "_intent_similarity", fail_cosine)
    result = _classify_intent("this is complete", "completion", MagicMock(), "embed-model")
    assert result is PredicateResult.MET


def test_classify_intent_falls_back_to_cosine_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unknown intent (None verdict) falls through to the cosine floor."""
    monkeypatch.setattr(classifier, "build_intent_scorer_from_env", lambda: MagicMock())
    monkeypatch.setattr(classifier, "_intent_rerank", lambda *_a, **_k: None)  # force fall-through
    floor = PredicateResult.NOT_MET
    monkeypatch.setattr(classifier, "_intent_similarity", lambda *_a, **_k: floor)
    result = _classify_intent("text", "completion", MagicMock(), "embed-model")
    assert result is floor


def test_eval_user_intent_routes_through_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: the predicate uses the reranker verdict when enabled."""
    monkeypatch.setattr(
        classifier, "build_intent_scorer_from_env", lambda: _scorer(yes_logprob=0.0)
    )
    ctx = PredicateContext(
        project_root=tmp_path,
        current_phase="spec",
        recent_prompt_text="this is complete, moving on",
    )
    result = eval_user_intent_matches({"intent": "completion"}, ctx, MagicMock(), "embed-model")
    assert result is PredicateResult.MET


def test_eval_user_intent_negation_vetoed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        classifier, "build_intent_scorer_from_env", lambda: _scorer(yes_logprob=0.0)
    )

    def fail_cosine(*_a: object, **_k: object) -> PredicateResult:
        raise AssertionError("negation veto is terminal; cosine must not run")

    monkeypatch.setattr(classifier, "_intent_similarity", fail_cosine)
    ctx = PredicateContext(
        project_root=tmp_path,
        current_phase="spec",
        recent_prompt_text="this is NOT done yet, gaps remain",
    )
    result = eval_user_intent_matches({"intent": "completion"}, ctx, MagicMock(), "embed-model")
    assert result is PredicateResult.NOT_MET


def test_reranker_response_matches_json_shape() -> None:
    """Guards against drift in the faked completion payload."""
    payload = _completion([("yes", -0.1), ("no", -2.0)])
    assert json.loads(json.dumps(payload))["choices"][0]["logprobs"]["content"][0]["top_logprobs"]
