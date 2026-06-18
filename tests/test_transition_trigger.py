"""Tests for ``check_transition_trigger`` — the reranker-primary phase-transition
trigger.

The semantic intent layer (``classifier._classify_intent``) is stubbed so these
stay hermetic and fast: the point here is the trigger's *wiring* (reranker
primary, deterministic keyword/artifact fallback floor), not the intent model
itself (covered in test_classifier_reranker.py / test_classifier_similarity.py).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

import agentalloy.signals.classifier as classifier
from agentalloy.signals.classifier import check_transition_trigger
from agentalloy.signals.predicates import PredicateContext, PredicateResult

# Representative spec-phase metadata.
_SPEC_KEYWORDS = ["done with spec", "ready to design", "next phase"]
_SPEC_GATES = {"all_of": [{"artifact_exists": {"path": "docs/spec/*.md"}}]}


def _ctx(prompt: str, tmp_path: Path) -> PredicateContext:
    return PredicateContext(
        project_root=tmp_path,
        current_phase="spec",
        recent_prompt_text=prompt,
    )


def test_intent_primary_fires_without_keyword(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Natural-language phrasing with no literal keyword still triggers."""
    monkeypatch.setattr(classifier, "_classify_intent", lambda *a, **k: PredicateResult.MET)
    ctx = _ctx("Looks right. Now the design.", tmp_path)
    match = check_transition_trigger(
        _SPEC_KEYWORDS, _SPEC_GATES, ctx, lm_client=MagicMock(), model="m"
    )
    assert match is not None
    assert match.name == "intent"


def test_falls_back_to_keyword_when_intent_not_met(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Intent NOT_MET but an explicit keyword present -> deterministic floor fires."""
    monkeypatch.setattr(classifier, "_classify_intent", lambda *a, **k: PredicateResult.NOT_MET)
    ctx = _ctx("I'm done with spec, moving on.", tmp_path)
    match = check_transition_trigger(
        _SPEC_KEYWORDS, _SPEC_GATES, ctx, lm_client=MagicMock(), model="m"
    )
    assert match is not None
    assert match.name == "prompt_keyword"


def test_no_match_when_neither_intent_nor_keyword(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(classifier, "_classify_intent", lambda *a, **k: PredicateResult.NOT_MET)
    ctx = _ctx("What does the auth flow look like?", tmp_path)
    match = check_transition_trigger(
        _SPEC_KEYWORDS, _SPEC_GATES, ctx, lm_client=MagicMock(), model="m"
    )
    assert match is None


def test_no_lm_client_is_pure_prefilter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a client the reranker can't run -> deterministic-only (no regression)."""
    called = False

    def _spy(*a: object, **k: object) -> PredicateResult:
        nonlocal called
        called = True
        return PredicateResult.MET

    monkeypatch.setattr(classifier, "_classify_intent", _spy)
    ctx = _ctx("Now the design.", tmp_path)  # no literal keyword
    match = check_transition_trigger(_SPEC_KEYWORDS, _SPEC_GATES, ctx, lm_client=None, model="m")
    assert match is None
    assert called is False  # intent path skipped when lm_client is None


def test_force_check_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTALLOY_FORCE_CHECK", "1")
    ctx = _ctx("anything at all", tmp_path)
    match = check_transition_trigger([], {}, ctx, lm_client=None, model="m")
    assert match is not None
    assert match.name == "manual"
