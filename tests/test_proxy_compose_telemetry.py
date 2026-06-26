"""Consolidated proxy compose telemetry.

Covers the two halves of the "one rich row per proxy request" design:

1. ``_merge_compose_telemetry`` folds the Tier 1 (system/header) and Tier 2
   (domain) compose results into a single ``ProxyComposeTelemetry`` — header
   skills (workflow + system), returned domain skills, summed tokens, and the
   Stage B kept/dropped/scores detail (from the domain leg only).
2. ``write_proxy_trace`` persists that detail to ``composition_traces`` and it
   round-trips through ``query_traces`` (incl. the new Stage B columns).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentalloy.api.compose_models import (
    ComposedResult,
    ComposeTelemetry,
    EmptyResult,
    LatencyBreakdown,
)
from agentalloy.api.proxy_apply import ProxyComposeTelemetry, _merge_compose_telemetry
from agentalloy.api.proxy_signal import SignalResult
from agentalloy.api.proxy_telemetry import write_proxy_trace
from agentalloy.storage.vector_store import VectorStore, open_or_create


def _tier1(
    workflow_skill_ids: list[str], system_fragments: list[str], tokens: int
) -> ComposedResult:
    """A system-only (header) compose result."""
    return ComposedResult(
        task="t",
        phase="build",
        output="header prose",
        domain_fragments=[],
        source_skills=[],
        system_fragments=system_fragments,
        system_skills_applied=True,
        assembly_tier=1,
        latency_ms=LatencyBreakdown(retrieval_ms=1, assembly_ms=0, total_ms=1),
        telemetry=ComposeTelemetry(
            tokens_returned=tokens,
            tokens_flat_equivalent=tokens * 3,
            workflow_skill_ids=workflow_skill_ids,
        ),
    )


def _tier2_with_stage_b() -> ComposedResult:
    """A domain compose result with a Stage B HIT (kept/dropped/scores)."""
    return ComposedResult(
        task="t",
        phase="build",
        output="domain prose",
        domain_fragments=["f-a", "f-b"],
        source_skills=["skill-a", "skill-b"],
        system_fragments=[],
        system_skills_applied=False,
        assembly_tier=1,
        latency_ms=LatencyBreakdown(retrieval_ms=2, assembly_ms=0, total_ms=2),
        telemetry=ComposeTelemetry(
            tokens_returned=100,
            tokens_flat_equivalent=400,
            reranked=True,
            lm_assist_outcome="hit",
            lm_assist_model="qwen3",
            lm_assist_kept_ids=["f-a", "f-b"],
            lm_assist_dropped_ids=["f-c"],
            lm_assist_scores={"f-a": 0.9, "f-b": 0.6, "f-c": 0.02},
        ),
    )


def test_merge_folds_header_returned_and_stage_b() -> None:
    signal = SignalResult(should_compose=True, phase="build", workflow_skill_id="wf-build")
    tier1 = _tier1(workflow_skill_ids=[], system_fragments=["sys-1", "sys-2"], tokens=10)
    tier2 = _tier2_with_stage_b()

    merged = _merge_compose_telemetry(signal, tier1, tier2)

    # Header: workflow skill (from the signal) + system fragments (Tier 1).
    assert merged.workflow_skill_ids == ["wf-build"]
    assert merged.header_fragment_ids == ["sys-1", "sys-2"]
    # Returned: the Tier 2 domain skills.
    assert merged.returned_skill_ids == ["skill-a", "skill-b"]
    # Every injected fragment, both tiers.
    assert merged.selected_fragment_ids == ["sys-1", "sys-2", "f-a", "f-b"]
    # Tokens summed across tiers.
    assert merged.tokens_returned == 110
    assert merged.tokens_flat_equivalent == 10 * 3 + 400
    # Stage B detail comes from the domain leg.
    assert merged.lm_assist_outcome == "hit"
    assert merged.reranked is True
    assert merged.lm_assist_kept_ids == ["f-a", "f-b"]
    assert merged.lm_assist_dropped_ids == ["f-c"]
    assert merged.lm_assist_scores == {"f-a": 0.9, "f-b": 0.6, "f-c": 0.02}


def test_merge_dedupes_workflow_skill_id_from_tier1() -> None:
    """If the Tier 1 telemetry already lists the workflow skill, the signal's id
    is not appended twice."""
    signal = SignalResult(should_compose=True, phase="build", workflow_skill_id="wf-build")
    tier1 = _tier1(workflow_skill_ids=["wf-build"], system_fragments=[], tokens=0)
    merged = _merge_compose_telemetry(signal, tier1, None)
    assert merged.workflow_skill_ids == ["wf-build"]


def test_merge_passthrough_both_legs_none_is_empty() -> None:
    signal = SignalResult(should_compose=False, phase="build")
    merged = _merge_compose_telemetry(signal, None, None)
    assert merged.returned_skill_ids == []
    assert merged.header_fragment_ids == []
    assert merged.selected_fragment_ids == []
    assert merged.tokens_returned == 0
    assert merged.lm_assist_outcome == "disabled"
    assert merged.lm_assist_kept_ids == []


def test_merge_empty_tier2_contributes_no_returned_skills() -> None:
    """A Tier 2 EmptyResult (no domain hits) still carries Stage B detail but no
    returned skills."""
    signal = SignalResult(should_compose=True, phase="build")
    tier2 = EmptyResult(
        task="t",
        phase="build",
        system_fragments=[],
        system_skills_applied=False,
        telemetry=ComposeTelemetry(lm_assist_outcome="hit", lm_assist_dropped_ids=["f-x"]),
    )
    merged = _merge_compose_telemetry(signal, None, tier2)
    assert merged.returned_skill_ids == []
    assert merged.lm_assist_outcome == "hit"
    assert merged.lm_assist_dropped_ids == ["f-x"]


@pytest.fixture
def store(tmp_path: Path) -> VectorStore:  # type: ignore[misc]
    with open_or_create(tmp_path / "t.duck") as s:
        yield s


def test_write_proxy_trace_persists_stage_b_and_skills(store: VectorStore) -> None:
    """The consolidated row round-trips header/returned skills and Stage B detail."""
    tel = ProxyComposeTelemetry(
        workflow_skill_ids=["wf-build"],
        header_fragment_ids=["sys-1"],
        returned_skill_ids=["skill-a", "skill-b"],
        selected_fragment_ids=["sys-1", "f-a", "f-b"],
        tokens_returned=110,
        tokens_flat_equivalent=430,
        reranked=True,
        dense_leg_degraded=False,
        lm_assist_outcome="hit",
        lm_assist_model="qwen3",
        lm_assist_kept_ids=["f-a", "f-b"],
        lm_assist_dropped_ids=["f-c"],
        lm_assist_scores={"f-a": 0.9, "f-c": 0.02},
    )
    write_proxy_trace(
        store,
        phase="build",
        task_prompt="implement feature X",
        status="proxy_composed",
        source_skill_ids=tel.returned_skill_ids,
        system_skill_ids=tel.header_fragment_ids,
        workflow_skill_ids=tel.workflow_skill_ids,
        selected_fragment_ids=tel.selected_fragment_ids,
        tokens_returned=tel.tokens_returned,
        tokens_flat_equivalent=tel.tokens_flat_equivalent,
        reranked=tel.reranked,
        lm_assist_outcome=tel.lm_assist_outcome,
        lm_assist_model=tel.lm_assist_model,
        lm_assist_kept_ids=tel.lm_assist_kept_ids,
        lm_assist_dropped_ids=tel.lm_assist_dropped_ids,
        lm_assist_scores=json.dumps(tel.lm_assist_scores),
        repo="/repo/a",
    )

    rows = store.query_traces(limit=10)
    assert len(rows) == 1
    row = rows[0]
    assert row.status == "proxy_composed"
    assert row.event_type == "proxy_request"
    assert row.source_skill_ids == ["skill-a", "skill-b"]
    assert row.system_skill_ids == ["sys-1"]
    assert row.workflow_skill_ids == ["wf-build"]
    assert row.lm_assist_outcome == "hit"
    assert row.lm_assist_kept_ids == ["f-a", "f-b"]
    assert row.lm_assist_dropped_ids == ["f-c"]
    assert json.loads(row.lm_assist_scores or "{}") == {"f-a": 0.9, "f-c": 0.02}

    # It counts as one compose in savings, with the summed tokens.
    savings = store.aggregate_savings()
    assert savings["total_composes"] == 1
    assert savings["tokens_returned"] == 110
    assert savings["tokens_flat_equivalent"] == 430
