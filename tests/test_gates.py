"""Gate aggregation and phase-transition decision tests."""

from __future__ import annotations

from pathlib import Path

from agentalloy.signals.gates import (
    _PHASE_GRAPH,
    _near_miss_candidates,
    aggregate,
    decide_transition,
    evaluate_node,
)
from agentalloy.signals.predicates import PredicateContext, PredicateResult

MET = PredicateResult.MET
NOT_MET = PredicateResult.NOT_MET
UNKNOWN = PredicateResult.UNKNOWN


def _ctx(tmp_path: Path, phase: str = "build") -> PredicateContext:
    return PredicateContext(project_root=tmp_path, current_phase=phase)


# ---------------------------------------------------------------------------
# aggregate
# ---------------------------------------------------------------------------


def test_all_of_all_met():
    assert aggregate("all_of", [MET, MET, MET]) == MET


def test_all_of_short_circuit_on_not_met():
    assert aggregate("all_of", [MET, NOT_MET, MET]) == NOT_MET


def test_all_of_unknown_no_not_met():
    assert aggregate("all_of", [MET, UNKNOWN]) == UNKNOWN


def test_any_of_short_circuit_on_met():
    assert aggregate("any_of", [NOT_MET, MET]) == MET


def test_any_of_all_not_met():
    assert aggregate("any_of", [NOT_MET, NOT_MET]) == NOT_MET


def test_any_of_unknown_no_met():
    assert aggregate("any_of", [NOT_MET, UNKNOWN]) == UNKNOWN


def test_not_met_inverts_to_met():
    assert aggregate("not", [MET]) == NOT_MET


def test_not_not_met_inverts_to_met():
    assert aggregate("not", [NOT_MET]) == MET


def test_not_unknown_stays_unknown():
    assert aggregate("not", [UNKNOWN]) == UNKNOWN


# ---------------------------------------------------------------------------
# test_unknown_propagates_correctly
# ---------------------------------------------------------------------------


def test_unknown_propagates_correctly_all_of(tmp_path: Path):
    ctx = _ctx(tmp_path)
    spec = {
        "all_of": [
            {"phase_in": {"phases": ["build"]}},  # MET
            {"artifact_exists": {"path": "nope.md"}},  # NOT_MET
        ]
    }
    qwen_calls: list[int] = [0]
    result, _ = evaluate_node(spec, ctx, None, qwen_calls)
    # Short-circuits on NOT_MET even though first is MET
    assert result == NOT_MET


def test_unknown_propagates_correctly_any_of(tmp_path: Path):
    ctx = _ctx(tmp_path)
    spec = {
        "any_of": [
            {"artifact_exists": {"path": "nope.md"}},  # NOT_MET
            {"artifact_exists": {"path": ""}},  # UNKNOWN (no path)
        ]
    }
    qwen_calls: list[int] = [0]
    result, _ = evaluate_node(spec, ctx, None, qwen_calls)
    assert result == UNKNOWN


# ---------------------------------------------------------------------------
# evaluate_node — nested composites
# ---------------------------------------------------------------------------


def test_nested_aggregates(tmp_path: Path):
    (tmp_path / "spec.md").write_text("## Acceptance Criteria\n\nhi\n")
    ctx = _ctx(tmp_path)
    spec = {
        "all_of": [
            {
                "any_of": [
                    {"artifact_exists": {"path": "spec.md"}},
                    {"artifact_exists": {"path": "nope.md"}},
                ]
            },
            {"not": {"artifact_exists": {"path": "definitely-missing.md"}}},
        ]
    }
    qwen_calls: list[int] = [0]
    result, _evals = evaluate_node(spec, ctx, None, qwen_calls)
    assert result == MET


# ---------------------------------------------------------------------------
# decide_transition
# ---------------------------------------------------------------------------


def test_decide_transition_writes_phase_atomically(tmp_path: Path):
    ctx = _ctx(tmp_path, phase="spec")
    # spec.md exists → artifact_exists gate MET
    (tmp_path / "spec.md").write_text("x" * 900)
    gate_spec = {"artifact_exists": {"path": "spec.md"}}
    decision = decide_transition("spec", gate_spec, ctx)
    assert decision.should_transition is True
    assert decision.to_phase == "design"
    assert decision.from_phase == "spec"

    # Atomic write
    phase_file = tmp_path / ".agentalloy" / "phase"
    phase_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_phase = phase_file.with_suffix(".tmp")
    tmp_phase.write_text("phase: design\n")
    tmp_phase.rename(phase_file)
    assert phase_file.read_text() == "phase: design\n"


def test_decide_transition_no_transition(tmp_path: Path):
    ctx = _ctx(tmp_path, phase="build")
    gate_spec = {"artifact_exists": {"path": "missing.md"}}
    decision = decide_transition("build", gate_spec, ctx)
    assert decision.should_transition is False
    assert decision.to_phase is None
    assert any(e.result == NOT_MET for e in decision.gates_unmet)


def test_decide_transition_next_phase_hint(tmp_path: Path):
    (tmp_path / "f.md").write_text("x")
    ctx = _ctx(tmp_path, phase="build")
    gate_spec = {"artifact_exists": {"path": "f.md"}}
    decision = decide_transition("build", gate_spec, ctx, next_phase_hint="special-phase")
    assert decision.to_phase == "special-phase"


def test_decide_transition_unknown_leaves_phase(tmp_path: Path):
    # current_phase=None makes phase_in return UNKNOWN → no transition
    ctx = PredicateContext(project_root=tmp_path, current_phase=None)
    gate_spec = {"phase_in": {"phases": ["build"]}}
    decision = decide_transition("build", gate_spec, ctx)
    assert decision.should_transition is False


def test_decide_transition_advises_missing_artifact(tmp_path: Path):
    """Trigger fired but the exit artifact is missing → advisory names the path
    and the target phase, so the agent knows what to produce to advance."""
    ctx = _ctx(tmp_path, phase="build")
    gate_spec = {"artifact_exists": {"path": "docs/spec/foo.md"}}
    decision = decide_transition("build", gate_spec, ctx)
    assert decision.should_transition is False
    assert any("docs/spec/foo.md" in a for a in decision.advisories)
    assert any("qa" in a for a in decision.advisories)  # _PHASE_GRAPH[build] == qa


def test_decide_transition_no_advisory_at_terminal_phase(tmp_path: Path):
    """The terminal phase (ship→ship) doesn't nag about a missing next artifact."""
    ctx = _ctx(tmp_path, phase="ship")
    gate_spec = {"artifact_exists": {"path": "never.md"}}
    decision = decide_transition("ship", gate_spec, ctx)
    assert decision.should_transition is False
    assert decision.advisories == []


# --- fast lane: sdd-fast → qa ----------------------------------------------

# The sdd-fast exit gate (mirrors src/agentalloy/_packs/sdd/sdd-fast.yaml):
# a combined fast brief carrying the compressed spec+design sections, plus
# the build's code and tests.
_SDD_FAST_GATE = {
    "all_of": [
        {"artifact_exists": {"path": "docs/fast/*.md"}},
        {
            "artifact_contains": {
                "path": "docs/fast/*.md",
                "sections": ["Acceptance Criteria", "Approach", "Test Cases"],
            }
        },
        {"artifact_exists": {"path": "src/**"}},
        {"artifact_exists": {"path": "tests/**/*.py"}},
    ]
}


def _seed_fast_artifacts(tmp_path: Path, *, sections: list[str]) -> None:
    fast = tmp_path / "docs" / "fast"
    fast.mkdir(parents=True)
    body = "\n".join(f"## {s}\n\ncontent\n" for s in sections)
    (fast / "task.md").write_text(body, encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "impl.py").write_text("x = 1\n", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_impl.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")


def test_phase_graph_sdd_fast_routes_to_qa():
    """The fast lane merges into the standard qa → ship, not straight to ship."""
    assert _PHASE_GRAPH["sdd-fast"] == "qa"


def test_decide_transition_sdd_fast_to_qa(tmp_path: Path):
    """Fast brief (all three sections) + code + tests advances sdd-fast → qa."""
    ctx = _ctx(tmp_path, phase="sdd-fast")
    _seed_fast_artifacts(tmp_path, sections=["Acceptance Criteria", "Approach", "Test Cases"])
    decision = decide_transition("sdd-fast", _SDD_FAST_GATE, ctx)
    assert decision.should_transition is True
    assert decision.to_phase == "qa"


def test_decide_transition_sdd_fast_missing_section_blocks(tmp_path: Path):
    """A fast brief missing the Approach section does not advance to qa."""
    ctx = _ctx(tmp_path, phase="sdd-fast")
    _seed_fast_artifacts(tmp_path, sections=["Acceptance Criteria", "Test Cases"])
    decision = decide_transition("sdd-fast", _SDD_FAST_GATE, ctx)
    assert decision.should_transition is False
    assert decision.to_phase is None


# --- near-miss deliverable detection ---------------------------------------


def test_near_miss_candidates_finds_misplaced_spec(tmp_path: Path):
    """A spec written to the repo root is a near-miss for `docs/spec/*.md`."""
    (tmp_path / "linkvault-spec.md").write_text("# spec\n")
    assert _near_miss_candidates(tmp_path, "docs/spec/*.md") == ["linkvault-spec.md"]


def test_near_miss_candidates_excludes_strict_matches(tmp_path: Path):
    """Files the strict glob already matches are not near-misses; misplaced ones are."""
    (tmp_path / "docs" / "spec").mkdir(parents=True)
    (tmp_path / "docs" / "spec" / "foo.md").write_text("# ok\n")
    (tmp_path / "bar-spec.md").write_text("# misplaced\n")
    assert _near_miss_candidates(tmp_path, "docs/spec/*.md") == ["bar-spec.md"]


def test_near_miss_candidates_skips_directory_glob(tmp_path: Path):
    """Directory-style globs (src/**, tests/**) have no meaningful 'wrong path'."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1\n")
    assert _near_miss_candidates(tmp_path, "src/**") == []


def test_decide_transition_near_miss_advisory(tmp_path: Path):
    """Reproduces the laptop case: spec at repo root → no transition + a sharp,
    actionable advisory naming the found file and where it belongs."""
    ctx = _ctx(tmp_path, phase="spec")
    (tmp_path / "linkvault2-spec.md").write_text("# spec\n")
    gate_spec = {"artifact_exists": {"path": "docs/spec/*.md"}}
    decision = decide_transition("spec", gate_spec, ctx)
    assert decision.should_transition is False
    advisory = "\n".join(decision.advisories)
    assert "linkvault2-spec.md" in advisory
    assert "docs/spec/*.md" in advisory
    assert "design" in advisory  # names the target phase
    assert "Move or rename" in advisory


# ---------------------------------------------------------------------------
# evaluate_gates (REMOVED: dead code — LOW-3)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# artifact_completeness advisory (Phase 6)
# ---------------------------------------------------------------------------


def test_artifact_completeness_gate_returns_unknown(tmp_path: Path):
    """artifact_completeness never blocks a transition — always UNKNOWN."""
    (tmp_path / "spec.md").write_text("# Spec\n\nsome content\n")
    ctx = _ctx(tmp_path)
    gate_spec = {"artifact_completeness": {"path": "spec.md", "criteria": "all ACs testable"}}
    _, evals = evaluate_node(gate_spec, ctx, None, [0])
    assert evals[0].result == UNKNOWN


def test_artifact_completeness_advisory_populated(tmp_path: Path):
    """Advisory text is built when artifact exists."""
    (tmp_path / "spec.md").write_text("# Spec\n\nsome content\n")
    ctx = _ctx(tmp_path)
    gate_spec = {"artifact_completeness": {"path": "spec.md", "criteria": "all ACs testable"}}
    _, evals = evaluate_node(gate_spec, ctx, None, [0])
    assert evals[0].advisory is not None
    assert "agentalloy-eval" in evals[0].advisory
    assert "all ACs testable" in evals[0].advisory


def test_artifact_completeness_advisory_omitted_when_no_file(tmp_path: Path):
    """Advisory is None when the artifact doesn't exist."""
    ctx = _ctx(tmp_path)
    gate_spec = {"artifact_completeness": {"path": "missing.md", "criteria": "x"}}
    _, evals = evaluate_node(gate_spec, ctx, None, [0])
    assert evals[0].advisory is None


def test_decide_transition_collects_advisories(tmp_path: Path):
    """decide_transition surfaces advisories in PhaseTransitionDecision."""
    (tmp_path / "spec.md").write_text("# content")
    ctx = _ctx(tmp_path)
    gate_spec = {"artifact_completeness": {"path": "spec.md", "criteria": "complete"}}
    decision = decide_transition("build", gate_spec, ctx)
    assert len(decision.advisories) == 1
    assert "agentalloy-eval" in decision.advisories[0]


def test_non_completeness_gate_has_no_advisory(tmp_path: Path):
    """Regular predicates produce no advisory."""
    (tmp_path / "f.md").write_text("hi")
    ctx = _ctx(tmp_path)
    gate_spec = {"artifact_exists": {"path": "f.md"}}
    _, evals = evaluate_node(gate_spec, ctx, None, [0])
    assert evals[0].advisory is None
