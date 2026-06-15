"""Tests for Phase 6/7 similarity-based semantic predicate evaluator."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from agentalloy.signals.classifier import (
    _INTENT_REFERENCES,  # pyright: ignore[reportPrivateUsage]
    _MAX_INPUT_CHARS,  # pyright: ignore[reportPrivateUsage]
    _SIMILARITY_THRESHOLD,  # pyright: ignore[reportPrivateUsage]
    SEMANTIC_PREDICATES,
    _cosine,  # pyright: ignore[reportPrivateUsage]
    _intent_similarity,  # pyright: ignore[reportPrivateUsage]
    _topic_similarity,  # pyright: ignore[reportPrivateUsage]
    eval_artifact_completeness,
    eval_prompt_topic_matches,
    eval_user_intent_matches,
)
from agentalloy.signals.predicates import PredicateResult


def _mock_client(vecs: list[list[float]]) -> MagicMock:
    client = MagicMock()
    client.embed.return_value = vecs
    return client


# ---------------------------------------------------------------------------
# _cosine
# ---------------------------------------------------------------------------


def test_cosine_identical_vectors() -> None:
    v = [1.0, 0.0, 0.0]
    assert _cosine(v, v) == pytest.approx(1.0)  # pyright: ignore[reportUnknownMemberType]


def test_cosine_orthogonal() -> None:
    assert _cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)  # pyright: ignore[reportUnknownMemberType]


def test_cosine_zero_vector() -> None:
    assert _cosine([0.0, 0.0], [1.0, 0.0]) == 0.0


# ---------------------------------------------------------------------------
# Phase 7: classifier query format
# ---------------------------------------------------------------------------


def test_intent_similarity_query_has_search_query_prefix(tmp_path: Path) -> None:
    """Query passed to embed carries the nomic ``search_query: `` prefix."""
    query = [1.0, 0.0]
    refs = [[0.99, 0.14], [0.0, 1.0]]
    client = _mock_client([query] + refs)
    _intent_similarity("done", "completion", client, "embed-model", threshold=0.56)

    call_args = client.embed.call_args
    texts: list[str] = call_args.kwargs.get("texts", [])
    assert texts[0] == "search_query: done"


def test_intent_similarity_references_have_search_query_prefix(tmp_path: Path) -> None:
    """Reference phrases carry the nomic ``search_query: `` prefix (symmetric task)."""
    query = [1.0, 0.0]
    refs = [[0.99, 0.14], [0.0, 1.0]]
    client = _mock_client([query] + refs)
    _intent_similarity("done", "completion", client, "embed-model", threshold=0.56)

    call_args = client.embed.call_args
    texts: list[str] = call_args.kwargs.get("texts", [])

    expected_refs = [f"search_query: {r}" for r in _INTENT_REFERENCES["completion"]]
    actual_refs = texts[1:]
    assert actual_refs == expected_refs


def test_intent_similarity_truncation(tmp_path: Path) -> None:
    """Long input is truncated before embedding; the nomic ``search_query: ``
    prefix is prepended after truncation (final = prefix + capped raw)."""
    query = [1.0, 0.0]
    refs = [[0.99, 0.14]]
    client = _mock_client([query] + refs)
    long_text = "x" * (_MAX_INPUT_CHARS + 500)
    _intent_similarity(long_text, "completion", client, "embed-model", threshold=0.56)

    call_args = client.embed.call_args
    texts: list[str] = call_args.kwargs.get("texts", [])
    assert len(texts[0]) == _MAX_INPUT_CHARS + len("search_query: ")
    assert texts[0].startswith("search_query: ")


def test_topic_similarity_query_has_search_query_prefix(tmp_path: Path) -> None:
    """Topic mode also carries the nomic ``search_query: `` prefix on query and topics."""
    query = [1.0, 0.0]
    topics = [[0.99, 0.14]]
    client = _mock_client([query] + topics)
    _topic_similarity("auth question", ["authentication"], client, "embed-model", threshold=0.56)

    call_args = client.embed.call_args
    texts: list[str] = call_args.kwargs.get("texts", [])
    assert texts[0] == "search_query: auth question"
    assert texts[1] == "search_query: authentication"


def test_unknown_intent_returns_unknown() -> None:
    """Unknown intent should return UNKNOWN without calling embed."""
    client = MagicMock()
    result = _intent_similarity("text", "nonexistent_intent", client, "embed-model")
    assert result == PredicateResult.UNKNOWN
    client.embed.assert_not_called()


def test_startup_validation_keys_match() -> None:
    """Verify that _INTENT_TASK_DESCRIPTIONS and _INTENT_REFERENCES have matching keys."""
    from agentalloy.signals.classifier import (  # noqa: PLC2701
        _INTENT_REFERENCES,  # pyright: ignore[reportPrivateUsage]
        _INTENT_TASK_DESCRIPTIONS,  # pyright: ignore[reportPrivateUsage]
    )

    assert set(_INTENT_TASK_DESCRIPTIONS.keys()) == set(_INTENT_REFERENCES.keys()), (
        "Keys should match: "
        f"{set(_INTENT_TASK_DESCRIPTIONS.keys())} != {set(_INTENT_REFERENCES.keys())}"
    )
    # Verify all three intents are present
    for intent in ["completion", "approval", "redirection"]:
        assert intent in _INTENT_REFERENCES
        assert intent in _INTENT_TASK_DESCRIPTIONS


# ---------------------------------------------------------------------------
# _intent_similarity
# ---------------------------------------------------------------------------


def test_intent_similarity_above_threshold_returns_met(tmp_path: Path) -> None:
    # query vec very similar to one of the refs
    query = [1.0, 0.0]
    refs = [[0.99, 0.14], [0.0, 1.0]]
    client = _mock_client([query] + refs)
    result = _intent_similarity("done", "completion", client, "embed-model", threshold=0.75)
    assert result == PredicateResult.MET


def test_intent_similarity_below_threshold_returns_not_met(tmp_path: Path) -> None:
    query = [1.0, 0.0]
    refs = [[0.0, 1.0], [0.0, 1.0]]  # all orthogonal
    client = _mock_client([query] + refs)
    result = _intent_similarity("done", "completion", client, "embed-model", threshold=0.75)
    assert result == PredicateResult.NOT_MET


def test_intent_similarity_embed_failure_returns_unknown() -> None:
    client = MagicMock()
    client.embed.side_effect = RuntimeError("connection refused")
    result = _intent_similarity("text", "completion", client, "embed-model")
    assert result == PredicateResult.UNKNOWN


# ---------------------------------------------------------------------------
# eval_artifact_completeness — always UNKNOWN
# ---------------------------------------------------------------------------


def test_artifact_completeness_always_unknown(tmp_path: Path) -> None:
    from agentalloy.signals.predicates import PredicateContext

    ctx = PredicateContext(
        project_root=tmp_path,
        current_phase="build",
        recent_prompt_text="",
    )
    client = MagicMock()
    result = eval_artifact_completeness(
        {"path": "spec.md", "criteria": "all ACs are testable"},
        ctx,
        client,
        "embed-model",
    )
    assert result == PredicateResult.UNKNOWN
    client.embed.assert_not_called()


def test_artifact_completeness_no_args_unknown(tmp_path: Path) -> None:
    from agentalloy.signals.predicates import PredicateContext

    ctx = PredicateContext(
        project_root=tmp_path,
        current_phase="build",
        recent_prompt_text="",
    )
    result = eval_artifact_completeness({}, ctx, MagicMock(), "embed-model")
    assert result == PredicateResult.UNKNOWN


# ---------------------------------------------------------------------------
# eval_user_intent_matches
# ---------------------------------------------------------------------------


def test_user_intent_matches_met(tmp_path: Path) -> None:
    from agentalloy.signals.predicates import PredicateContext

    query = [1.0, 0.0]
    refs = [[0.99, 0.14], [0.0, 1.0]]
    client = _mock_client([query] + refs)
    ctx = PredicateContext(
        project_root=tmp_path,
        current_phase="build",
        recent_prompt_text="looks good, approve",
    )
    result = eval_user_intent_matches({"intent": "completion"}, ctx, client, "embed-model")
    assert result == PredicateResult.MET


def test_user_intent_matches_empty_prompt_unknown(tmp_path: Path) -> None:
    from agentalloy.signals.predicates import PredicateContext

    ctx = PredicateContext(
        project_root=tmp_path,
        current_phase="build",
        recent_prompt_text="",
    )
    result = eval_user_intent_matches({"intent": "completion"}, ctx, MagicMock(), "embed-model")
    assert result == PredicateResult.UNKNOWN


# ---------------------------------------------------------------------------
# eval_prompt_topic_matches
# ---------------------------------------------------------------------------


def test_prompt_topic_matches_met(tmp_path: Path) -> None:
    from agentalloy.signals.predicates import PredicateContext

    query = [1.0, 0.0]
    refs = [[0.99, 0.14]]
    client = _mock_client([query] + refs)
    ctx = PredicateContext(
        project_root=tmp_path,
        current_phase="build",
        recent_prompt_text="let's discuss authentication",
    )
    result = eval_prompt_topic_matches({"topics": ["authentication"]}, ctx, client, "embed-model")
    assert result == PredicateResult.MET


def test_prompt_topic_matches_empty_topics_unknown(tmp_path: Path) -> None:
    from agentalloy.signals.predicates import PredicateContext

    ctx = PredicateContext(
        project_root=tmp_path,
        current_phase="build",
        recent_prompt_text="something",
    )
    result = eval_prompt_topic_matches({"topics": []}, ctx, MagicMock(), "embed-model")
    assert result == PredicateResult.UNKNOWN


# ---------------------------------------------------------------------------
# SEMANTIC_PREDICATES registry
# ---------------------------------------------------------------------------


def test_semantic_predicates_no_chat_calls(tmp_path: Path) -> None:
    """Verify none of the semantic predicates use lm_client.chat."""
    from agentalloy.signals.predicates import PredicateContext

    ctx = PredicateContext(
        project_root=tmp_path,
        current_phase="build",
        recent_prompt_text="done",
    )
    client = MagicMock()
    client.embed.return_value = [[1.0, 0.0]] * 15

    for name, fn in SEMANTIC_PREDICATES.items():
        if name == "artifact_completeness":
            fn({"path": "f.md", "criteria": "x"}, ctx, client, "m")
        elif name in ("user_intent_matches", "agent_intent_matches"):
            fn({"intent": "completion"}, ctx, client, "m")
        elif name == "prompt_topic_matches":
            fn({"topics": ["auth"]}, ctx, client, "m")

    client.chat.assert_not_called()


# ---------------------------------------------------------------------------
# Phase 7: Calibration fixture validation
# ---------------------------------------------------------------------------


def test_calibration_fixture_valid() -> None:
    """Validate the calibration fixture meets minimum requirements."""
    fixture_path = Path(__file__).parent / "fixtures" / "classifier_calibration.jsonl"
    assert fixture_path.exists(), f"Fixture not found at {fixture_path}"

    examples: list[dict[str, Any]] = []
    with open(fixture_path) as f:
        for line in f:
            line = line.strip()
            if line:
                examples.append(json.loads(line))

    assert len(examples) >= 60, f"Need >= 60 examples, got {len(examples)}"

    intent_counts: dict[str, int] = {}
    for ex in examples:
        intent = ex.get("intent", "")
        intent_counts[intent] = intent_counts.get(intent, 0) + 1

    for intent in ["completion", "approval", "redirection"]:
        assert intent_counts.get(intent, 0) >= 15, (
            f"Need >= 15 examples for intent '{intent}', got {intent_counts.get(intent, 0)}"
        )

    assert intent_counts.get("none", 0) >= 15, (
        f"Need >= 15 negative examples, got {intent_counts.get('none', 0)}"
    )


# ---------------------------------------------------------------------------
# Phase 7: Regression test (requires live embed server)
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="requires live embed server with specific model loaded")
def test_classifier_regression_against_live_server() -> None:
    """Regression test: F1 >= 0.85, false_met_rate <= 0.05 at committed threshold."""
    from agentalloy.config import get_settings
    from agentalloy.lm_client import OpenAICompatClient

    settings = get_settings()
    embed_url = settings.runtime_embed_base_url
    model = settings.runtime_embedding_model

    if not embed_url:
        pytest.skip("No embed server configured")

    fixture_path = Path(__file__).parent / "fixtures" / "classifier_calibration.jsonl"
    examples: list[dict[str, Any]] = []
    with open(fixture_path) as f:
        for line in f:
            line = line.strip()
            if line:
                examples.append(json.loads(line))

    lm_client = OpenAICompatClient(base_url=embed_url)
    threshold = _SIMILARITY_THRESHOLD

    # Run the classifier against all examples
    tp = fp = fn = tn = 0
    for ex in examples:
        text = ex["text"]
        target_intent = ex["intent"]

        if target_intent == "none":
            # Check that no intent fires MET
            any_met = False
            for intent in ["completion", "approval", "redirection"]:
                result = _intent_similarity(text, intent, lm_client, model, threshold=threshold)
                if result == PredicateResult.MET:
                    any_met = True
                    break
            if any_met:
                fp += 1
            else:
                tn += 1
        else:
            # Check that the target intent fires MET
            result = _intent_similarity(text, target_intent, lm_client, model, threshold=threshold)
            if result == PredicateResult.MET:
                tp += 1
            else:
                fn += 1

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
    false_met_rate = fp / (fp + tn) if (fp + tn) > 0 else 0.0

    print(f"Regression test results at threshold={threshold:.2f}:")
    print(f"  TP={tp}, FP={fp}, TN={tn}, FN={fn}")
    print(f"  Precision: {precision:.3f}")
    print(f"  Recall:    {recall:.3f}")
    print(f"  F1:        {f1:.3f}")
    print(f"  False MET: {false_met_rate:.3f}")

    assert f1 >= 0.85, f"F1 {f1:.3f} < 0.85"
    assert false_met_rate <= 0.05, f"False MET rate {false_met_rate:.3f} > 0.05"
