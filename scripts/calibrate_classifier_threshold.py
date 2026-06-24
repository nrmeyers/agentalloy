#!/usr/bin/env python3
"""Calibrate the classifier similarity threshold against a held-out fixture set.

Usage:
    uv run python scripts/calibrate_classifier_threshold.py

Requirements:
    - Embed server must be running (configured per Settings)
    - Fixture file must exist at tests/fixtures/classifier_calibration.jsonl

Output:
    - Threshold sweep table with precision, recall, F1, false_met_rate
    - Confusion matrix at recommended threshold
    - Machine-grep-able line: RECOMMENDED_THRESHOLD=X.XX

Exits non-zero if no threshold satisfies false_met_rate <= 0.05.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# Add src to path so we can import the classifier module
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from agentalloy.config import get_settings
from agentalloy.lm_client import OpenAICompatClient
from agentalloy.signals.classifier import (
    _INTENT_REFERENCES,  # pyright: ignore[reportPrivateUsage]
)
from agentalloy.signals.predicates import PredicateContext


def load_fixture(path: Path) -> list[dict[str, Any]]:
    """Load the calibration fixture JSONL file."""
    examples: list[dict[str, Any]] = []
    with open(path) as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                examples.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"ERROR: Invalid JSON on line {line_num}: {e}", file=sys.stderr)
                sys.exit(1)
    return examples


def validate_fixture(examples: list[dict[str, Any]]) -> None:
    """Validate fixture meets minimum requirements."""
    if len(examples) < 60:
        print(f"ERROR: Need >= 60 examples, got {len(examples)}", file=sys.stderr)
        sys.exit(1)

    intent_counts: dict[str, int] = {}
    for ex in examples:
        intent = ex.get("intent", "")
        intent_counts[intent] = intent_counts.get(intent, 0) + 1

    for intent in ["completion", "approval", "redirection"]:
        if intent_counts.get(intent, 0) < 15:
            print(
                f"ERROR: Need >= 15 examples for intent '{intent}', "
                f"got {intent_counts.get(intent, 0)}",
                file=sys.stderr,
            )
            sys.exit(1)

    if intent_counts.get("none", 0) < 15:
        print(
            f"ERROR: Need >= 15 negative examples (intent='none'), "
            f"got {intent_counts.get('none', 0)}",
            file=sys.stderr,
        )
        sys.exit(1)


def compute_metrics(
    examples: list[dict[str, Any]],
    ctx: PredicateContext,
    lm_client: OpenAICompatClient,
    model: str,
    threshold: float,
) -> dict[str, Any]:
    """Compute precision, recall, F1, false_met_rate for a given threshold."""
    from agentalloy.signals.classifier import (  # noqa: PLC2701
        _MAX_INPUT_CHARS,  # pyright: ignore[reportPrivateUsage]
        _cosine,  # pyright: ignore[reportPrivateUsage]
    )

    # Pre-compute all similarity scores (one embed call per example)
    # Mirror classifier._intent_similarity: truncate and embed raw text + refs
    all_scores: dict[int, dict[str, float]] = {}
    for idx, ex in enumerate(examples):
        text = ex["text"]
        ctx.recent_prompt_text = text
        scores: dict[str, float] = {}
        for intent in ["completion", "approval", "redirection"]:
            refs = _INTENT_REFERENCES[intent]  # type: ignore[index]
            query = text[:_MAX_INPUT_CHARS]
            try:
                vecs = lm_client.embed(model=model, texts=[query] + refs)
                query_vec = vecs[0]
                best = max(_cosine(query_vec, r) for r in vecs[1:])
                scores[intent] = best
            except Exception as e:
                print(f"WARNING: Embed failed for '{text[:50]}...': {e}", file=sys.stderr)
                scores[intent] = 0.0
        all_scores[idx] = scores

    # Evaluate at the given threshold
    tp = 0  # True positives: correctly identified intent
    fp = 0  # False positives: intent fired when it shouldn't
    fn = 0  # False negatives: intent missed when it should have fired
    tn = 0  # True negatives: correctly identified no intent

    for idx, ex in enumerate(examples):
        scores = all_scores[idx]
        if ex["intent"] == "none":
            # Negative example: should NOT have any intent return MET
            any_met = any(
                scores[intent] >= threshold for intent in ["completion", "approval", "redirection"]
            )
            if any_met:
                fp += 1  # False positive: fired when it shouldn't
            else:
                tn += 1  # True negative: correctly didn't fire
        else:
            # Named intent: check if the correct intent fired MET
            target_intent = ex["intent"]
            target_met = scores[target_intent] >= threshold
            other_met = any(
                scores[i] >= threshold
                for i in ["completion", "approval", "redirection"]
                if i != target_intent
            )
            if target_met and not other_met:
                tp += 1
            elif not target_met and not other_met:
                fn += 1
            elif target_met and other_met:
                # Ambiguous: target met but so did another intent
                tp += 1
            else:
                # Other intent met but not target
                fn += 1

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

    # False MET rate: fraction of negative examples that incorrectly fired
    total_negatives = fp + tn
    false_met_rate = fp / total_negatives if total_negatives > 0 else 0.0

    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "false_met_rate": false_met_rate,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
    }


def print_confusion_matrix(metrics: dict[str, Any], threshold: float) -> None:
    """Print a confusion matrix for the given metrics."""
    print(f"\nConfusion Matrix at threshold={threshold:.2f}:")
    print(f"  True Positives:  {metrics['tp']}")
    print(f"  False Positives: {metrics['fp']}")
    print(f"  True Negatives:  {metrics['tn']}")
    print(f"  False Negatives: {metrics['fn']}")
    print(f"  Precision: {metrics['precision']:.3f}")
    print(f"  Recall:    {metrics['recall']:.3f}")
    print(f"  F1:        {metrics['f1']:.3f}")
    print(f"  False MET rate: {metrics['false_met_rate']:.3f}")


def main() -> None:
    # Load fixture
    fixture_path = (
        Path(__file__).parent.parent / "tests" / "fixtures" / "classifier_calibration.jsonl"
    )
    if not fixture_path.exists():
        print(f"ERROR: Fixture not found at {fixture_path}", file=sys.stderr)
        sys.exit(1)

    examples = load_fixture(fixture_path)
    validate_fixture(examples)
    print(f"Loaded {len(examples)} calibration examples")

    # Get embed client
    settings = get_settings()
    model = settings.runtime_embedding_model
    embed_url = settings.runtime_embed_base_url

    if not embed_url:
        print("ERROR: No embed URL configured in settings", file=sys.stderr)
        sys.exit(1)

    print(f"Embed server: {embed_url}, model: {model}")

    # Create a mock context for evaluation
    ctx = PredicateContext(
        project_root=Path(__file__).parent.parent,
        current_phase="build",
        recent_prompt_text="",
    )

    lm_client = OpenAICompatClient(base_url=embed_url)

    # Sweep thresholds
    print("\nThreshold sweep:")
    print(f"{'Threshold':>10} {'Precision':>10} {'Recall':>10} {'F1':>10} {'False MET':>10}")
    print("-" * 55)

    best_threshold: float | None = None
    best_f1 = 0.0
    best_metrics: dict[str, Any] = {}

    for i in range(50, 96):  # 0.50 to 0.95
        threshold = i / 100.0
        metrics = compute_metrics(examples, ctx, lm_client, model, threshold)

        print(
            f"{threshold:>10.2f} "
            f"{metrics['precision']:>10.3f} "
            f"{metrics['recall']:>10.3f} "
            f"{metrics['f1']:>10.3f} "
            f"{metrics['false_met_rate']:>10.3f}"
        )

        # Check if this threshold meets the constraint
        if metrics["false_met_rate"] <= 0.05 and metrics["f1"] > best_f1:
            best_f1 = metrics["f1"]
            best_threshold = threshold
            best_metrics = metrics

    if best_threshold is None:
        print("\nERROR: No threshold satisfies false_met_rate <= 0.05", file=sys.stderr)
        print(
            "Consider expanding reference phrases or revising task descriptions.", file=sys.stderr
        )
        sys.exit(1)

    print(f"\nRecommended threshold: {best_threshold:.2f}")
    print(f"  F1: {best_metrics['f1']:.3f}")
    print(f"  False MET rate: {best_metrics['false_met_rate']:.3f}")

    print_confusion_matrix(best_metrics, best_threshold)

    # Machine-grep-able output
    print(f"\nRECOMMENDED_THRESHOLD={best_threshold:.2f}")


if __name__ == "__main__":
    main()
