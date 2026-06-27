"""Stage B — LM fragment re-ranker.

No model downloads, no live network: the FragmentScorer is driven through a
faked httpx transport (fixed logprobs → known score), and the pipeline-
integration tests fake the scorer factory. The headline guarantee is fail-open
parity: with LM_ASSIST unset, retrieval is byte-identical to today's
deterministic selection (same pattern as the off-mode tests in
tests/test_card_index.py).
"""

from __future__ import annotations

import json
import math

import httpx
import pytest

import agentalloy.retrieval.domain as domain_module
import agentalloy.retrieval.lm_assist as lm_assist
from agentalloy.reads.models import ActiveFragment
from agentalloy.retrieval.domain import _maybe_lm_arbitrate  # pyright: ignore[reportPrivateUsage]
from agentalloy.retrieval.lm_assist import (
    FragmentScorer,
    LMAssistMode,
    LMAssistOutcome,
    build_prompt,
    build_scorer_from_env,
    load_config,
    reset_lm_assist_cache,
    score_from_logprobs,
)
from agentalloy.retrieval.lm_assist import (
    _parse_completion_logprobs as parse_logprobs,  # pyright: ignore[reportPrivateUsage]
)

_LM_ENV = (
    "LM_ASSIST",
    "LM_ASSIST_RERANK_URL",
    "LM_ASSIST_TIMEOUT_MS",
    "LM_ASSIST_KEEP_THRESHOLD",
    "LM_ASSIST_MODEL",
    "LM_ASSIST_MAX_CANDIDATES",
    "LM_ASSIST_DOC_CAP_CHARS",
)


@pytest.fixture(autouse=True)
def _clean_lm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _LM_ENV:
        monkeypatch.delenv(name, raising=False)
    reset_lm_assist_cache()
    yield
    reset_lm_assist_cache()


# -------- config --------


def test_config_defaults_off() -> None:
    cfg = load_config()
    assert cfg.mode is LMAssistMode.OFF
    assert cfg.enabled is False
    assert cfg.url == "http://127.0.0.1:47952"
    assert cfg.timeout_ms == 600
    # Gated-off default is TRULY inert: 0.0 keeps every score>=0 (D6 measure-then-set;
    # 0.05 would empty a task whose candidates all score 0.0 — found in live test).
    assert cfg.keep_threshold == pytest.approx(0.0)
    assert cfg.model == "Qwen3-Reranker-0.6B-Q8_0.gguf"


def test_config_arbitrate_and_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LM_ASSIST", "arbitrate")
    monkeypatch.setenv("LM_ASSIST_RERANK_URL", "http://host:9/")
    monkeypatch.setenv("LM_ASSIST_TIMEOUT_MS", "250")
    monkeypatch.setenv("LM_ASSIST_KEEP_THRESHOLD", "0.3")
    monkeypatch.setenv("LM_ASSIST_MODEL", "tag-x")
    cfg = load_config()
    assert cfg.enabled is True
    assert cfg.url == "http://host:9"  # trailing slash stripped
    assert cfg.timeout_ms == 250
    assert cfg.keep_threshold == pytest.approx(0.3)
    assert cfg.model == "tag-x"


def test_config_unknown_mode_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LM_ASSIST", "enrich")  # not supported yet
    assert load_config().mode is LMAssistMode.OFF


def test_factory_off_returns_none() -> None:
    assert build_scorer_from_env() is None


# -------- prompt template --------


def test_build_prompt_uses_qwen_reranker_template() -> None:
    p = build_prompt("my task", "my doc")
    assert p.startswith("<|im_start|>system\n")
    assert "<Instruct>:" in p and "<Query>: my task" in p and "<Document>: my doc" in p
    # The assistant prefix must end with the empty think block — required for
    # the model to emit yes/no as the very next token.
    assert p.endswith("<|im_start|>assistant\n<think>\n\n</think>\n\n")


# -------- scoring math --------


def test_score_from_logprobs_softmax() -> None:
    # P(yes) = e^0 / (e^0 + e^{-1}) for logprobs yes=0, no=-1.
    score = score_from_logprobs({"yes": 0.0, "no": -1.0})
    expected = 1.0 / (1.0 + math.exp(-1.0))
    assert score == pytest.approx(expected)


def test_score_from_logprobs_case_and_whitespace_insensitive() -> None:
    # llama.cpp emits a leading space; "Yes"/"NO" variants fold into the classes.
    score = score_from_logprobs({" yes": -0.1, "No": -5.0})
    assert score > 0.95


def test_score_from_logprobs_no_class_tokens_is_zero() -> None:
    assert score_from_logprobs({"maybe": -0.1, "1": -0.2}) == 0.0


# -------- response parsing --------


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


def test_parse_completion_logprobs_happy() -> None:
    out = parse_logprobs(_completion([("yes", -0.1), ("no", -2.0)]))
    assert out == {"yes": -0.1, "no": -2.0}


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"choices": []},
        {"choices": [{}]},
        {"choices": [{"logprobs": {}}]},
        {"choices": [{"logprobs": {"content": []}}]},
    ],
)
def test_parse_completion_logprobs_malformed_raises(payload: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        parse_logprobs(payload)


# -------- FragmentScorer (faked transport) --------


def _scorer_with_responses(responses: dict[str, float], *, timeout_ms: int = 300) -> FragmentScorer:
    """Build a scorer whose httpx client is backed by a MockTransport that
    returns a fixed yes-logprob per document substring."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        prompt = body["prompt"]
        # Find which document this prompt is for and return its scripted score.
        for needle, yes_lp in responses.items():
            if needle in prompt:
                return httpx.Response(200, json=_completion([("yes", yes_lp), ("no", -10.0)]))
        return httpx.Response(200, json=_completion([("no", 0.0)]))

    cfg = load_config()
    cfg = cfg.__class__(
        mode=LMAssistMode.ARBITRATE,
        url="http://test",
        timeout_ms=timeout_ms,
        keep_threshold=0.05,
        model="m",
    )
    scorer = FragmentScorer(cfg)
    scorer._client = httpx.Client(  # pyright: ignore[reportPrivateUsage]
        transport=httpx.MockTransport(handler), base_url="http://test"
    )
    return scorer


def test_fragment_scorer_known_scores() -> None:
    scorer = _scorer_with_responses({"ALPHA": 0.0, "BRAVO": -10.0})
    try:
        result = scorer.score("task", ["doc ALPHA", "doc BRAVO"])
    finally:
        scorer.close()
    assert result.outcome is LMAssistOutcome.HIT
    # ALPHA: yes=0, no=-10 → ~1.0; BRAVO: yes=-10, no=-10 → 0.5.
    assert result.scores[0] > 0.99
    assert result.scores[1] == pytest.approx(0.5, abs=0.01)


def test_fragment_scorer_empty_input_is_hit() -> None:
    scorer = _scorer_with_responses({})
    try:
        result = scorer.score("task", [])
    finally:
        scorer.close()
    assert result.outcome is LMAssistOutcome.HIT
    assert result.scores == []


def test_fragment_scorer_connection_error_fails_open() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    cfg = load_config().__class__(
        mode=LMAssistMode.ARBITRATE,
        url="http://test",
        timeout_ms=300,
        keep_threshold=0.05,
        model="m",
    )
    scorer = FragmentScorer(cfg)
    scorer._client = httpx.Client(  # pyright: ignore[reportPrivateUsage]
        transport=httpx.MockTransport(boom), base_url="http://test"
    )
    try:
        result = scorer.score("task", ["doc"])
    finally:
        scorer.close()
    assert result.outcome is LMAssistOutcome.ERROR
    assert result.scores == []


# -------- pipeline integration: _maybe_lm_arbitrate --------


def _frag(fid: str, skill_id: str, content: str = "body") -> ActiveFragment:
    return ActiveFragment(
        fragment_id=fid,
        fragment_type="execution",
        sequence=1,
        content=content,
        skill_id=skill_id,
        version_id=f"{skill_id}-v1",
        skill_class="domain",
        category="design",
        domain_tags=[],
    )


class _FakeScorer:
    def __init__(self, outcome: LMAssistOutcome, scores: list[float]) -> None:
        self._result = lm_assist.ScoreResult(outcome, scores)

    def score(self, task: str, documents: list[str]) -> lm_assist.ScoreResult:  # noqa: ARG002
        return self._result


def test_arbitrate_disabled_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # No scorer built (LM_ASSIST off) → fall through to deterministic.
    monkeypatch.setattr(domain_module, "build_scorer_from_env", lambda: None)
    selected, outcome, detail = _maybe_lm_arbitrate([_frag("f1", "s1")], "task", k=4)
    assert selected is None
    assert outcome is LMAssistOutcome.DISABLED
    assert detail is None


def test_arbitrate_threshold_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    ranked = [_frag("f1", "s1"), _frag("f2", "s2"), _frag("f3", "s3")]
    monkeypatch.setattr(
        domain_module,
        "build_scorer_from_env",
        lambda: _FakeScorer(LMAssistOutcome.HIT, [0.9, 0.01, 0.6]),
    )
    monkeypatch.setattr(domain_module, "load_config", lambda: _cfg(0.05))
    selected, outcome, detail = _maybe_lm_arbitrate(ranked, "task", k=4)
    assert outcome is LMAssistOutcome.HIT
    assert selected is not None
    # f2 (0.01) drops; order preserved by fusion rank (f1 then f3).
    assert [f.fragment_id for f in selected] == ["f1", "f3"]
    # Telemetry detail: kept = injected, dropped = below-threshold, scores over all.
    assert detail is not None
    assert detail.kept_ids == ["f1", "f3"]
    assert detail.dropped_ids == ["f2"]
    assert detail.scores == {"f1": 0.9, "f2": 0.01, "f3": 0.6}


def test_arbitrate_empty_keep(monkeypatch: pytest.MonkeyPatch) -> None:
    # All below threshold → keep [] (valid "inject nothing"), NOT None.
    ranked = [_frag("f1", "s1"), _frag("f2", "s2")]
    monkeypatch.setattr(
        domain_module,
        "build_scorer_from_env",
        lambda: _FakeScorer(LMAssistOutcome.HIT, [0.0, 0.001]),
    )
    monkeypatch.setattr(domain_module, "load_config", lambda: _cfg(0.05))
    selected, outcome, detail = _maybe_lm_arbitrate(ranked, "task", k=4)
    assert outcome is LMAssistOutcome.HIT
    assert selected == []
    # Nothing kept; both scored fragments land in dropped.
    assert detail is not None
    assert detail.kept_ids == []
    assert detail.dropped_ids == ["f1", "f2"]


def test_arbitrate_returns_all_survivors_uncapped(monkeypatch: pytest.MonkeyPatch) -> None:
    # §D: _maybe_lm_arbitrate is now a FILTER, not a fusion-order cap. It returns
    # every above-threshold survivor (uncapped); the k cap is applied downstream by
    # skill_granular_select at the call site, NOT here.
    ranked = [_frag(f"f{i}", f"s{i}") for i in range(6)]
    monkeypatch.setattr(
        domain_module,
        "build_scorer_from_env",
        lambda: _FakeScorer(LMAssistOutcome.HIT, [0.9] * 6),
    )
    monkeypatch.setattr(domain_module, "load_config", lambda: _cfg(0.05))
    selected, _, detail = _maybe_lm_arbitrate(ranked, "task", k=2)
    assert selected is not None
    # All six clear the threshold and all six are returned despite k=2 (no trim here).
    assert len(selected) == 6
    assert detail is not None
    assert len(detail.kept_ids) == 6
    assert detail.dropped_ids == []
    assert len(detail.scores) == 6


def test_arbitrate_timeout_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    ranked = [_frag("f1", "s1")]
    monkeypatch.setattr(
        domain_module,
        "build_scorer_from_env",
        lambda: _FakeScorer(LMAssistOutcome.TIMEOUT, []),
    )
    selected, outcome, detail = _maybe_lm_arbitrate(ranked, "task", k=4)
    assert selected is None
    assert outcome is LMAssistOutcome.TIMEOUT
    assert detail is None


def test_arbitrate_length_mismatch_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    ranked = [_frag("f1", "s1"), _frag("f2", "s2")]
    monkeypatch.setattr(
        domain_module,
        "build_scorer_from_env",
        lambda: _FakeScorer(LMAssistOutcome.HIT, [0.9]),  # one score for two docs
    )
    selected, outcome, detail = _maybe_lm_arbitrate(ranked, "task", k=4)
    assert selected is None
    assert outcome is LMAssistOutcome.ERROR
    assert detail is None


def _cfg(threshold: float) -> lm_assist.LMAssistConfig:
    return lm_assist.LMAssistConfig(
        mode=LMAssistMode.ARBITRATE,
        url="http://test",
        timeout_ms=300,
        keep_threshold=threshold,
        model="m",
    )


# -------- §C: doc-cap, pool bound, per-request timeout --------


def test_score_one_truncates_document_to_doc_cap() -> None:
    # A document longer than doc_cap_chars must be truncated BEFORE build_prompt, so
    # only the first doc_cap_chars characters reach the posted /v1/completions prompt.
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured["prompt"] = body["prompt"]
        return httpx.Response(200, json=_completion([("yes", 0.0), ("no", -10.0)]))

    cfg = lm_assist.LMAssistConfig(
        mode=LMAssistMode.ARBITRATE,
        url="http://test",
        timeout_ms=300,
        keep_threshold=0.05,
        model="m",
        doc_cap_chars=2400,
    )
    scorer = FragmentScorer(cfg)
    scorer._client = httpx.Client(  # pyright: ignore[reportPrivateUsage]
        transport=httpx.MockTransport(handler), base_url="http://test"
    )
    document = "X" * 3000  # 600 chars over the cap
    try:
        result = scorer.score("task", [document])
    finally:
        scorer.close()
    assert result.outcome is LMAssistOutcome.HIT
    # Exactly doc_cap_chars 'X's survived into the prompt; the 600-char tail is gone.
    assert captured["prompt"].count("X") == 2400
    assert "X" * 2400 in captured["prompt"]
    assert "X" * 2401 not in captured["prompt"]


def test_pool_width_equals_max_candidates() -> None:
    # The scorer thread pool is keyed to the same knob as the candidate cap, so the
    # pool width can never drift from --parallel. Default == 8.
    scorer = FragmentScorer(_cfg(0.05))
    try:
        assert scorer._pool._max_workers == lm_assist.max_candidates()  # pyright: ignore[reportPrivateUsage]
        assert scorer._pool._max_workers == 8  # pyright: ignore[reportPrivateUsage]
    finally:
        scorer.close()


def test_max_candidates_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LM_ASSIST_MAX_CANDIDATES", "6")
    assert lm_assist.max_candidates() == 6
    scorer = FragmentScorer(_cfg(0.05))
    try:
        # Pool width follows the override too (one knob, both sites).
        assert scorer._pool._max_workers == 6  # pyright: ignore[reportPrivateUsage]
    finally:
        scorer.close()


def test_per_req_timeout_under_batch_budget() -> None:
    # Per-request httpx timeout is strictly under the batch budget (0.9x) so one
    # hung request can't consume the whole budget before the deadline loop reaps it.
    cfg = lm_assist.LMAssistConfig(
        mode=LMAssistMode.ARBITRATE,
        url="http://test",
        timeout_ms=1000,
        keep_threshold=0.05,
        model="m",
    )
    scorer = FragmentScorer(cfg)
    try:
        assert scorer._client.timeout.read == pytest.approx(0.9)  # pyright: ignore[reportPrivateUsage]
        assert scorer._client.timeout.read < 1000 / 1000.0  # pyright: ignore[reportPrivateUsage]
    finally:
        scorer.close()
