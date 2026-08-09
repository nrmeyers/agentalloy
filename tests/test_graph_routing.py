# pyright: reportPrivateUsage=false
"""Tests for graph node→prose binding and routing parity (tasks 03–04).

Covers test-plan.md T5–T9 and the "routing identical" parity property that lets
the graph ship: for every phase/lane, ``route_step`` must reproduce the
post-#551 ``_PHASE_GRAPH``/``next_phase_hint`` outcome.
"""

from __future__ import annotations

import pytest

from agentalloy.signals.graph import (
    _PHASE_GRAPH,
    build_phase_graph,
    phase_node,
)
from agentalloy.signals.graph import (
    _route_step as route_step,
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


def test_route_from_decision_forwards() -> None:
    from agentalloy.signals.graph import route_from_decision

    class _D:
        should_transition = True
        to_phase = "design"
        advisories = []
        gates_met = []
        gates_unmet = []
        qwen_calls = 0

    out = route_from_decision(_D(), "spec", "sdd-full")
    assert out.should_transition is True
    assert out.to_phase == "design"


def test_route_from_decision_self_loops_when_blocked() -> None:
    from agentalloy.signals.graph import route_from_decision

    class _D:
        should_transition = False
        to_phase = "design"
        advisories = ["not done"]
        gates_met = []
        gates_unmet = []
        qwen_calls = 0

    out = route_from_decision(_D(), "spec", "sdd-full")
    assert out.should_transition is False
    assert "not done" in out.advisories


# ---------------------------------------------------------------------------
# T24 — subagent hard-constraint: no Send/dispatch, prose is thread-independent
# ---------------------------------------------------------------------------


def test_t24_node_prose_is_thread_independent() -> None:
    """T24 — a subagent sharing the parent's thread_id gets the same prose.

    The hard constraint (approach.md §4) is that no node assumes a fresh context
    window or forks off a child: ``phase_node`` is a pure function of the phase,
    never of the thread/checkpoint. So two invocations, whatever the state, must
    resolve identical prose for the same phase.
    """
    a = phase_node("design")
    b = phase_node("design")
    assert a == b  # pure read — same phase, same prose, no thread dependence
    assert a is not None
    assert a["raw_prose"] == b["raw_prose"]


def test_t24_graph_never_dispatches_send() -> None:
    """T24 — the built graph contains no ``Send``/dispatch node.

    Subagents must not be spawned by the graph (the proxy drives subagents
    explicitly); no node may return a ``Send`` command. Assert the invariant on
    the node-construction source rather than langgraph's internal wrappers
    (which differ across versions and are not a stable introspection surface).
    """
    import inspect

    from agentalloy.signals import graph as graph_mod

    mod_src = inspect.getsource(graph_mod)
    assert "Send" not in mod_src
    assert "Command" not in mod_src  # no node returns a langgraph dispatch command
    # build_phase_graph registers only prose-binding nodes, never a dispatcher.
    build_src = inspect.getsource(build_phase_graph)
    assert "add_node" in build_src


# ---------------------------------------------------------------------------
# T10 — HTTP path through graph + gate block via graph
# ---------------------------------------------------------------------------


def test_http_target_phase_overrides_auto_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """T10 — when the HTTP /state/phase path sends target_phase, it overrides _PHASE_GRAPH."""
    import agentalloy.signals.graph as graph

    # Gate always passes when called
    monkeypatch.setattr(graph, "evaluate_phase_gate", lambda *a, **k: _ALLOW)

    # Without target_phase: spec → design (normal _PHASE_GRAPH route)
    out = route_step("spec", "sdd-full")
    assert out.to_phase == "design"
    assert out.should_transition is True

    # With target_phase: spec → qa (bypasses design + plan + build)
    out = route_step("spec", "sdd-full", target_phase="qa")
    assert out.to_phase == "qa"
    assert out.should_transition is True


def test_gateway_block_returns_self_loop_with_advisories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T10 — when evaluate_phase_gate blocks, route_step returns should_transition=False."""
    import agentalloy.signals.graph as graph

    # Gate blocks with an advisory message
    block_verdict = {"result": "blocked", "advisories": ["missing: docs/spec/approach.artifact"]}
    monkeypatch.setattr(graph, "evaluate_phase_gate", lambda *a, **k: block_verdict)

    out = route_step("spec", "sdd-full")
    assert out.should_transition is False
    assert out.to_phase == "design"  # still reports the *intended* target
    assert "missing: docs/spec/approach.artifact" in out.advisories


def test_gateway_block_prevents_transition_for_all_phases(monkeypatch: pytest.MonkeyPatch) -> None:
    """T10 — gate block stops transition for every non-terminal phase."""
    import agentalloy.signals.graph as graph

    blocked = {"result": "blocked", "advisories": ["not done"]}
    monkeypatch.setattr(graph, "evaluate_phase_gate", lambda *a, **k: blocked)

    for phase in ["intake", "spec", "design", "plan", "build", "qa"]:
        out = route_step(phase, "sdd-full")
        assert out.should_transition is False, f"expected block for {phase}"


def test_gateway_override_skips_completeness(monkeypatch: pytest.MonkeyPatch) -> None:
    """T10 — override=True makes evaluate_phase_gate return None (allowed).

    override=True does NOT skip the gate function entirely — evaluate_phase_gate
    is still called, but it returns None (gate met) when override=True, so the
    transition proceeds.
    """
    import agentalloy.signals.graph as graph

    call_count = [0]

    def mock_gate(*a, **k):
        call_count[0] += 1
        if k.get("override") or (len(a) > 3 and a[3]):
            return _ALLOW  # override=True → gate met
        return {"result": "blocked", "advisories": ["not done"]}

    monkeypatch.setattr(graph, "evaluate_phase_gate", mock_gate)

    # override=True → gate returns None → transition allowed
    out = route_step("spec", "sdd-full", override=True)
    assert out.should_transition is True
    assert call_count[0] == 1


def test_gateway_approval_gate_not_skipped_by_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """T10 — override=True skips completeness but NOT the approval gate.

    The approval gate is a separate check inside evaluate_phase_gate.
    When approval is required and not recorded, the verdict is blocked
    even with override=True.
    """
    import agentalloy.signals.graph as graph

    # Approval gate blocks
    approval_block = {
        "result": "blocked",
        "advisories": [
            "'spec' is complete and awaiting human approval. PRESENT the work in full and STOP"
        ],
    }

    call_count = [0]

    def mock_gate(*a, **k):
        call_count[0] += 1
        return approval_block

    monkeypatch.setattr(graph, "evaluate_phase_gate", mock_gate)

    # Even with override=True, approval gate still blocks
    out = route_step("spec", "sdd-full", override=True)
    assert out.should_transition is False
    assert call_count[0] == 1
