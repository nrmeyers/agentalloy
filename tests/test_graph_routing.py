# pyright: reportPrivateUsage=false
"""Tests for graph node→prose binding and routing parity (tasks 03–04).

Covers test-plan.md T5–T9 and the "routing identical" parity property that lets
the graph ship: for every phase/lane, ``route_step`` must reproduce the
post-#551 ``_PHASE_GRAPH``/``next_phase_hint`` outcome.
"""

from __future__ import annotations

import pytest

from agentalloy.signals.gates import _PHASE_GRAPH
from agentalloy.signals.graph import (
    build_phase_graph,
    phase_node,
    route_step,
)

_ALLOW = None  # evaluate_phase_gate returning None == gate met (fail open)


# ---------------------------------------------------------------------------
# T5/T6 — node → prose binding (pure read)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "phase",
    [
        "intake",
        "spec",
        "design",
        "plan",
        "build",
        "qa",
        "ship",
        "sdd-fast",
        "add-skill",
        "sdd-flow",
    ],
)
def test_t5_phase_node_resolves_packaged_prose(phase: str) -> None:
    skill = phase_node(phase)
    # Every phase has a packaged workflow skill (sdd-<phase> prose must exist).
    assert skill is not None, f"no workflow skill for phase {phase}"
    assert skill.get("skill_class") == "workflow"
    assert phase in (skill.get("applies_to_phases") or [])


def test_t6_flow_lane_binds_flow_prose() -> None:
    skill = phase_node("sdd-flow")
    assert skill is not None
    assert "sdd-flow" in str(skill.get("skill_id", ""))


# ---------------------------------------------------------------------------
# T7/T8 — edge predicate adapter
# ---------------------------------------------------------------------------


def test_t7_gate_met_forwards(monkeypatch: pytest.MonkeyPatch) -> None:
    import agentalloy.signals.graph as graph

    monkeypatch.setattr(graph, "evaluate_phase_gate", lambda *a, **k: _ALLOW)
    out = route_step("spec", "sdd-full")
    assert out.should_transition is True
    assert out.to_phase == "design"
    assert out.from_phase == "spec"


def test_t8_gate_unmet_self_loops_with_advisories(monkeypatch: pytest.MonkeyPatch) -> None:
    import agentalloy.signals.graph as graph

    monkeypatch.setattr(
        graph,
        "evaluate_phase_gate",
        lambda *a, **k: {"result": "not_met", "advisories": ["produce the spec.md"]},
    )
    out = route_step("spec", "sdd-full")
    assert out.should_transition is False
    assert out.to_phase == "design"
    assert "produce the spec.md" in out.advisories


# ---------------------------------------------------------------------------
# T9 + parity — lane select & routing identical to _PHASE_GRAPH
# ---------------------------------------------------------------------------


def test_t9_intake_lane_selects_lane_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    import agentalloy.signals.graph as graph

    monkeypatch.setattr(graph, "evaluate_phase_gate", lambda *a, **k: _ALLOW)
    assert route_step("intake", "sdd-full").to_phase == "spec"
    assert route_step("intake", "sdd-fast").to_phase == "sdd-fast"
    assert route_step("intake", "add-skill").to_phase == "add-skill"
    assert route_step("intake", "flow").to_phase == "sdd-flow"


def test_next_phase_hint_overrides_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    import agentalloy.signals.graph as graph

    monkeypatch.setattr(graph, "evaluate_phase_gate", lambda *a, **k: _ALLOW)
    # An explicit hint out of intake wins over the derived lane entry.
    assert route_step("intake", "sdd-full", next_phase_hint="sdd-fast").to_phase == "sdd-fast"


def test_routing_parity_with_phase_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    """For every phase, the graph reproduces _PHASE_GRAPH exactly when unblocked."""
    import agentalloy.signals.graph as graph

    monkeypatch.setattr(graph, "evaluate_phase_gate", lambda *a, **k: _ALLOW)
    for phase, nxt in _PHASE_GRAPH.items():
        lane = (
            "add-skill" if phase == "add-skill" else "flow" if phase == "sdd-flow" else "sdd-full"
        )
        out = route_step(phase, lane)
        expected = nxt if phase != "intake" else "spec"
        assert out.to_phase == expected, f"{phase}: got {out.to_phase}, want {expected}"


def test_terminal_ship_stays_put(monkeypatch: pytest.MonkeyPatch) -> None:
    import agentalloy.signals.graph as graph

    monkeypatch.setattr(graph, "evaluate_phase_gate", lambda *a, **k: _ALLOW)
    out = route_step("ship", "sdd-full")
    assert out.should_transition is False
    assert out.to_phase == "ship"  # terminal self-loop — never advances past ship


# ---------------------------------------------------------------------------
# Topology inspectable (T22 preview)
# ---------------------------------------------------------------------------


def test_graph_topology_inspectable(monkeypatch: pytest.MonkeyPatch) -> None:
    import agentalloy.signals.graph as graph

    monkeypatch.setattr(graph, "evaluate_phase_gate", lambda *a, **k: _ALLOW)
    compiled = build_phase_graph().compile()
    nodes = set(compiled.get_graph().nodes.keys())
    for phase in _PHASE_GRAPH:
        assert phase in nodes, f"node {phase} missing from topology"
