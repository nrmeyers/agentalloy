"""LangGraph reactive phase graph — graph state and stream key.

Implements task 02 of ``docs/design/langgraph-graph-core`` (and approach.md
§1 / §3): the durable graph-state schema and the stream-scoped checkpointer key
(``thread_id``). This module deliberately does **not** build the StateGraph
topology yet — that arrives in tasks 03/04 — and it never re-implements
slugging or stream resolution; it composes the existing canonical primitives.

The frame (approach.md §0): the graph is *reactive, not driving*. No node calls
a model. An inbound proxy request pumps the graph; conditional-edge predicates
decide routing. AgentAlloy never originates a model call.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agentalloy.code_index.slug import repo_slug
from agentalloy.signals.gates import evaluate_phase_gate
from agentalloy.signals.skill_loader import (
    _load_workflow_skill_for_phase,  # noqa: PLC0415  # pyright: ignore[reportPrivateUsage]
)
from agentalloy.storage.state_store import (
    LEASED_KINDS,
    DuckDBStateStore,
    PhaseState,
)
from agentalloy.storage.stream_id import resolve_stream_id

# Canonical SDD phase map: phase → next phase (approach.md §1 / §7).
# Re-exported as ``_PHASE_GRAPH`` so callers that import from here get the
# same dict object — no copy, no stale reference.
_NEXT: dict[str, str] = {
    "intake": "spec",  # default (full) route; fast / add-skill / flow use lane hints
    "spec": "design",
    "design": "plan",
    "plan": "build",
    "build": "qa",
    "qa": "ship",
    "sdd-fast": "qa",  # compressed spec+design+build → merge into qa → ship
    "add-skill": "intake",  # deliverable is a locally installed skill, not shippable
    "sdd-flow": "intake",  # deliberately unguided exploration — return to intake
    "ship": "ship",  # terminal
}
# Legacy alias — imported by web/ops_api, install/subcommands/phase, and
# the LangGraph topology via route_step.  Both point to the SAME dict.
_PHASE_GRAPH = _NEXT

_log = logging.getLogger(__name__)


# Session-scoped delivery bookkeeping that is explicitly NOT graph state
# (approach.md §1; state_store.SESSION_SCOPED_KINDS). Pulling these into graph
# state would force ``thread_id = session`` — exactly the collapse #548 removes.
_NON_GRAPH_SESSION_KINDS: frozenset[str] = frozenset(
    {"announced", "composed", "banner-turns", "pause-reminded"},
)

# The four lanes (approach.md §1, §7 step 3 / coordination.md). ``sdd-full`` is
# the default linear sprint; the other three are the ``next_phase_hint`` routes.
_LANES: frozenset[str] = frozenset({"sdd-full", "sdd-fast", "add-skill", "flow"})

# Lane → entry phase once selected out of intake (derived from the finalized
# post-#551 topology; a new lane appears automatically, no hand-listed set).
_LANE_ENTRY: dict[str, str] = {
    "sdd-full": "spec",
    "sdd-fast": "sdd-fast",
    "add-skill": "add-skill",
    "flow": "sdd-flow",  # lane `flow` → phase `sdd-flow` (post-#551 naming)
}


class PhaseGraphState(TypedDict, total=False):
    """Durable graph state (approach.md §1). Source of truth for the graph.

    Explicitly **excludes** session-scoped delivery bookkeeping
    (``announced``/``composed``/``banner-turns``/``pause-reminded``), which stay
    per-session on the existing store.
    """

    phase: str  # intake, spec, design, plan, build, qa, ship, sdd-fast, add-skill, flow
    lane: str  # sdd-full | sdd-fast | add-skill | flow
    paused: bool  # workflow pause flag (#550) — not a node
    paused_since: str | None  # (#550) ISO-8601 timestamp (passthrough from store)
    contract_id: str | None  # active contract ref (if any)
    cursor: str | None  # current work-item slug
    approved: bool  # approval-marker validity (staleness-aware)
    artifact_refs: list[str]  # artifact refs produced this phase


@dataclass(frozen=True)
class ThreadKey:
    """The durable checkpointer key: ``(repo_slug, stream_id)``.

    ``repo_slug`` stays worktree-independent (code index); ``stream_id`` adds
    per-worktree isolation (#548). The two halves answer opposite questions —
    same code, one index; separate streams, separate phase state (approach.md §3).
    """

    repo_slug: str
    stream_id: str

    def as_tuple(self) -> tuple[str, str]:
        return (self.repo_slug, self.stream_id)


def make_thread_key(project_root: Path) -> ThreadKey:
    """Resolve the ``(repo_slug, stream_id)`` checkpointer key for *project_root*.

    Read-only composition of the two canonical resolvers — never re-implements
    slugging or stream resolution. Resolution order for ``stream_id`` is the
    post-#548 plumbing: explicit binding (``.agentalloy/.stream`` / env), else
    the worktree-path hash.
    """
    return ThreadKey(
        repo_slug=repo_slug(project_root),
        stream_id=resolve_stream_id(project_root),
    )


def leased_graph_kinds() -> frozenset[str]:
    """State kinds that participate in lease-based concurrency on the graph.

    Mirrors ``state_store.LEASED_KINDS`` semantics — the graph checkpoint lease
    protects the same owner-grained row kinds, now scoped per stream.
    """
    return LEASED_KINDS


def initial_phase_graph_state(*, phase: str = "intake", lane: str = "sdd-full") -> PhaseGraphState:
    """A fresh, empty graph state for a repo entering the graph."""
    return PhaseGraphState(
        phase=phase,
        lane=lane,
        paused=False,
        paused_since=None,
        contract_id=None,
        cursor=None,
        approved=False,
        artifact_refs=[],
    )


def to_phase_graph_state(phase_state: PhaseState | None) -> PhaseGraphState:
    """Map a store ``PhaseState`` row into graph state (approach.md §1).

    The store row owns ``phase``/``mode``/``paused_since``; the graph-level
    ``lane``/``cursor``/``approved``/``artifact_refs`` are folded from the same
    read or left at defaults where the row does not carry them. Session-scoped
    kinds are never surfaced here.
    """
    if phase_state is None:
        return PhaseGraphState(
            phase="intake",
            lane="sdd-full",
            paused=False,
            paused_since=None,
        )
    mode = phase_state.mode
    lane = mode if mode in _LANES else "sdd-full"
    paused = mode == "paused"
    return PhaseGraphState(
        phase=phase_state.phase,
        lane=lane,
        paused=paused,
        paused_since=phase_state.paused_since,
    )


# ---------------------------------------------------------------------------
# Task 05 — checkpoint persistence on the existing store
# ---------------------------------------------------------------------------

# The store kind that carries graph checkpoints, keyed by (repo, stream_id)
# via a scoped view. Independent of the session-scoped delivery kinds.
GRAPH_CHECKPOINT_KIND = "graph_checkpoint"


def save_graph_state(store: DuckDBStateStore, key: ThreadKey, state: PhaseGraphState) -> None:
    """Persist ``state`` for stream ``key`` on the existing store (approach.md §4).

    Writes a ``graph_checkpoint`` row into a ``(repo, stream_id)``-scoped view,
    so distinct streams never share a checkpoint row (#548) and the lease is
    scoped per stream.
    """
    view = store.for_repo(key.repo_slug, stream_id=key.stream_id)
    view.write(GRAPH_CHECKPOINT_KIND, json.dumps(dict(state)))


def load_graph_state(store: DuckDBStateStore, key: ThreadKey) -> PhaseGraphState | None:
    """Load the persisted graph state for stream ``key``, or ``None`` (fresh)."""
    view = store.for_repo(key.repo_slug, stream_id=key.stream_id)
    raw = view.read(GRAPH_CHECKPOINT_KIND)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    # pyright: ignore[reportUnknownArgumentType] — LangGraph PhaseGraphState stubs have unknown param types
    return PhaseGraphState(**{k: data[k] for k in PhaseGraphState.__annotations__ if k in data})  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Task 03 — node → prose binding (pure read, never a model call)
# ---------------------------------------------------------------------------


def phase_node(phase: str, cwd: Path | None = None) -> dict[str, Any] | None:
    """Resolve the workflow skill for ``phase`` (approach.md §2).

    Pure read — no mutation, no model/embedding call. Returns the packaged
    ``sdd-<phase>`` workflow skill (``exit_gates``, ``applies_to_phases``,
    ``contract_template``, ``signal_keywords``, ``raw_prose``) or ``None`` when
    the phase has no packaged workflow skill.
    """
    return _load_workflow_skill_for_phase(phase, cwd=cwd)


# ---------------------------------------------------------------------------
# Task 04 — edge predicate adapter + topology
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoutingOutcome:
    """What a gate evaluation means for routing (approach.md §2, §5)."""

    should_transition: bool
    from_phase: str
    to_phase: str | None
    lane: str
    advisories: list[str]
    reason: str = ""
    gates_met: list[str] = field(default_factory=list)  # type: ignore[assignment]
    gates_unmet: list[str] = field(default_factory=list)  # type: ignore[assignment]
    qwen_calls: int = 0


def _route_step(
    current_phase: str,
    lane: str,
    *,
    next_phase_hint: str | None = None,
    target_phase: str | None = None,
    override: bool = False,
    project_root: Path | None = None,
    store: Any = None,
) -> RoutingOutcome:
    """Decide the next phase for ``current_phase`` — the graph's routing key.

    Wraps ``evaluate_phase_gate`` (the deterministic core) + lane selection.
    From ``intake`` the lane (or ``next_phase_hint``) selects the lane entry;
    elsewhere forward edges are guarded by the gate — on ``should_transition``
    advance, else self-loop re-emit with advisories carried through. Declared
    non-linear edges (``flow → intake``, ``add-skill → intake``) route directly.

    When ``target_phase`` is provided, it overrides the automatic lookup
    (``_PHASE_GRAPH`` / lane entry) so callers can evaluate a specific
    transition — used by the HTTP / CLI path which receives the user's
    requested phase directly.

    When ``override`` is ``True``, the forward (completeness) gate is
    skipped but the approval gate (for phases in ``_ALWAYS_APPROVAL_PHASES``)
    is still enforced.
    """
    if lane not in _LANES:
        lane = "sdd-full"
    if target_phase is not None:
        to_phase = target_phase
    elif current_phase == "intake":
        to_phase = next_phase_hint or _LANE_ENTRY.get(lane)
    else:
        to_phase = _PHASE_GRAPH.get(current_phase)

    if to_phase is None:  # terminal (ship) or unknown → stay put
        return RoutingOutcome(False, current_phase, current_phase, lane, [])
    if to_phase == current_phase:  # same-phase no-op → skip gate entirely
        return RoutingOutcome(False, current_phase, current_phase, lane, [])

    verdict = evaluate_phase_gate(
        current_phase,
        to_phase,
        project_root,
        override=override,
        store=store,
    )
    if verdict is not None:
        # Gate blocked — pull gates_met/gates_unmet/qwen_calls via decide_transition
        # (evaluate_phase_gate already calls it but discards those fields).
        gates_met: list[str] = []
        gates_unmet: list[str] = []
        qwen_calls = 0
        if project_root:
            try:
                from agentalloy.signals.gates import (  # noqa: PLC0415
                    decide_transition,
                )
                from agentalloy.signals.predicates import (  # noqa: PLC0415
                    PredicateContext,
                )
                from agentalloy.signals.skill_loader import (  # noqa: PLC0415
                    exit_gates_for_phase,
                )

                gate_spec = exit_gates_for_phase(current_phase)
                if gate_spec:
                    decision_ctx = PredicateContext(
                        project_root=project_root,
                        current_phase=current_phase,
                        store=store,
                    )
                    decision = decide_transition(
                        current_phase,
                        gate_spec,
                        decision_ctx,
                        lm_client=None,
                        target_phase=to_phase,
                    )
                    gates_met = [e.gate_name for e in decision.gates_met]  # type: ignore[assignment]
                    gates_unmet = [e.gate_name for e in decision.gates_unmet]  # type: ignore[assignment]
                    qwen_calls = decision.qwen_calls
            except Exception:
                _log.debug(
                    "failed to resolve gate details for %s → %s",
                    current_phase,
                    to_phase,
                    exc_info=True,
                )
        return RoutingOutcome(
            False,
            current_phase,
            to_phase,
            lane,
            verdict.get("advisories") or [],
            verdict.get("result", "not_met"),
            gates_met=gates_met,
            gates_unmet=gates_unmet,
            qwen_calls=qwen_calls,
        )
    return RoutingOutcome(True, current_phase, to_phase, lane, [])


def route_from_decision(
    decision: Any,
    current_phase: str,
    lane: str = "sdd-full",
) -> RoutingOutcome:
    """Adapt an already-computed ``PhaseTransitionDecision`` to a routing key.

    Task 06 ("adapt PhaseTransitionDecision to a routing key; no rewrite"). The
    proxy's evaluation core is ``decide_transition`` (rich result — gates_met /
    gates_unmet / qwen_calls / advisories); this maps that result onto the
    graph's routing surface so the phase write is driven by one decision point
    without re-running the gate or duplicating ``_PHASE_GRAPH`` branching.
    """
    if lane not in _LANES:
        lane = "sdd-full"
    if not decision.should_transition or not decision.to_phase:
        return RoutingOutcome(
            False,
            current_phase,
            decision.to_phase,
            lane,
            list(decision.advisories),
            gates_met=[e.gate_name for e in decision.gates_met],
            gates_unmet=[e.gate_name for e in decision.gates_unmet],
            qwen_calls=decision.qwen_calls,
        )
    return RoutingOutcome(
        True,
        current_phase,
        decision.to_phase,
        lane,
        list(decision.advisories),
        gates_met=[e.gate_name for e in decision.gates_met],
        gates_unmet=[e.gate_name for e in decision.gates_unmet],
        qwen_calls=decision.qwen_calls,
    )


def _node(phase: str):
    """A StateGraph node for ``phase``: resolve prose, update state phase to self."""

    def _run(state: PhaseGraphState) -> PhaseGraphState:
        phase_node(phase)  # pure read — binds prose for the proxy to inject
        state["phase"] = phase  # keep graph-phase in sync with the executing node
        return state

    return _run


def build_phase_graph() -> StateGraph[PhaseGraphState]:
    """Assemble the reactive phase topology (approach.md §2 diagram).

    One node per phase; a conditional edge out of ``intake`` selects the lane;
    forward edges mirror ``_PHASE_GRAPH`` with ``route_step`` as predicate.
    ``ship`` is terminal. Returns an *uncompiled* graph — callers choose whether
    and how to checkpointer it (task 05).
    """
    g = StateGraph[PhaseGraphState](PhaseGraphState)
    for phase in _PHASE_GRAPH:
        g.add_node(phase, _node(phase))  # pyright: ignore[reportUnknownMemberType]
    g.add_edge(START, "intake")  # entrypoint — intake is the front door

    def _advance(state: PhaseGraphState) -> str:
        _d: dict[str, Any] = dict(state)
        phase = _d["phase"]
        nxt = _PHASE_GRAPH.get(phase)
        if nxt is None or nxt == phase:  # terminal or unknown phase → stay
            return phase
        return nxt

    g.add_conditional_edges(  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        "intake",
        lambda s: s.get("lane", "sdd-full"),  # type: ignore[arg-type]
        {lane: entry for lane, entry in _LANE_ENTRY.items()},
    )
    for phase, nxt in _PHASE_GRAPH.items():
        if phase == "intake" or phase == nxt:
            continue  # intake handled above; self-loops terminate at END below
        g.add_conditional_edges(phase, _advance, {phase: phase, nxt: nxt})
    g.add_edge("ship", END)
    return g


# ---------------------------------------------------------------------------
# Compiled-graph helper (module-level singleton)
# ---------------------------------------------------------------------------

_graph_compilation: (
    CompiledStateGraph[PhaseGraphState, None, PhaseGraphState, PhaseGraphState] | None
) = None  # type: ignore[type-arg]
_graph_phases: list[str] | None = None


def phase_graph() -> CompiledStateGraph[PhaseGraphState, None, PhaseGraphState, PhaseGraphState]:  # type: ignore[type-arg]
    """Return a compiled LangGraph ``StateGraph`` with checkpointer.

    The graph is compiled once per Python process so that every caller
    shares the same ``threading.Lock`` inside ``MemorySaver`` and
    checkpoint writes/reads are mutually consistent.
    """
    global _graph_compilation
    if _graph_compilation is None:
        _graph_compilation = build_phase_graph().compile(  # pyright: ignore[reportUnknownMemberType]
            checkpointer=MemorySaver()
        )
    return _graph_compilation


def all_phases() -> list[str]:
    """Return every phase node in the graph (intake, spec, design, …, sdd-flow)."""
    global _graph_phases
    if _graph_phases is None:
        _graph_phases = list(_PHASE_GRAPH.keys())
    return _graph_phases


__all__ = [
    "PhaseGraphState",
    "ThreadKey",
    "RoutingOutcome",
    "GRAPH_CHECKPOINT_KIND",
    "save_graph_state",
    "load_graph_state",
    "make_thread_key",
    "leased_graph_kinds",
    "initial_phase_graph_state",
    "to_phase_graph_state",
    "phase_node",
    "_route_step",
    "route_from_decision",
    "build_phase_graph",
    "phase_graph",
    "all_phases",
    "_LANES",
    "_LANE_ENTRY",
    "_NON_GRAPH_SESSION_KINDS",
]
