"""Phase machine: LangGraph outer graph for SDD lifecycle management.

Orchestrates the interpreter through lifecycle phases:
intake → spec → design → plan → build → qa → ship.

Key features:
- intake is the lifecycle front door (route decision: full / fast / add-skill)
- Approval gates at spec→design, design→plan, plan→build (AC-9);
  intake→spec is exit-artifact gated but not approval gated
- Exit artifact required before gate can pass
- Digest invalidation: editing an approved artifact voids the approval
- Send-based task fan-out for parallel work in build/qa phases
- interrupt() for human-in-the-loop approval (compiled with a checkpointer
  so interrupt/resume actually works)
"""

from __future__ import annotations

import itertools
import sqlite3
from collections.abc import Hashable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from agentalloy.state_store import LIFECYCLE_START, StateStore

# Explicit re-export — state_store is the source of truth for the lifecycle.
from agentalloy.state_store import PHASE_ORDER as PHASE_ORDER

Phase = Literal["intake", "spec", "design", "plan", "build", "qa", "ship"]

# Phase transitions that require human approval (AC-9)
APPROVAL_GATES: set[str] = {"spec→design", "design→plan", "plan→build"}

# Adjacent (current, target) pairs along the lifecycle.
_TRANSITIONS = list(itertools.pairwise(PHASE_ORDER))


@dataclass
class PhaseState:
    """State for the phase machine graph."""

    current_phase: str = "intake"
    messages: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str | None = None
    pending_approval: str | None = None
    task_results: list[dict[str, Any]] = field(default_factory=list)


class PhaseMachine:
    """LangGraph-based phase machine with approval gates."""

    def __init__(self, state_store: StateStore) -> None:
        self.state_store = state_store
        self.graph = self._build_graph()

    def _make_checkpointer(self) -> Any:
        """Persistent checkpointer next to the state store (AC-11:
        crash-resumable) — a run paused at an approval interrupt() must
        survive a process restart, which MemorySaver cannot provide.
        Falls back to MemorySaver only if the sqlite file cannot open.
        """
        try:
            path = Path(self.state_store.db_path).with_name("phase_checkpoints.sqlite")
            conn = sqlite3.connect(str(path), check_same_thread=False)
            return SqliteSaver(conn)
        except Exception:
            return MemorySaver()

    def _build_graph(self) -> Any:
        """Build the LangGraph state graph.

        Topology (mirrors the v1 reactive phase graph): START → router →
        <current phase> → gate_check → {next phase | approval_gate | END}.
        The graph is compiled with a checkpointer so the approval_gate's
        interrupt() can be resumed.
        """
        workflow = StateGraph(PhaseState)

        # Add a node for each phase
        for phase in PHASE_ORDER:
            workflow.add_node(phase, self._make_phase_node(phase))

        # Entry point: router routes to the current phase (from the store)
        workflow.add_edge(START, "router")
        workflow.add_node("router", self._router_node)
        workflow.add_conditional_edges(
            "router",
            self._route_to_current,
            {phase: phase for phase in PHASE_ORDER},
        )

        # Each phase routes to the gate checker
        for phase in PHASE_ORDER:
            workflow.add_edge(phase, "gate_check")

        # Gate checker decides: advance, interrupt, or end. Maps are derived
        # from PHASE_ORDER so the topology tracks the lifecycle.
        gate_map: dict[Hashable, str] = {
            f"advance_{phase}": PHASE_ORDER[i + 1] for i, phase in enumerate(PHASE_ORDER[:-1])
        }
        gate_map.update(
            {
                f"approve_{phase}→{target}": "approval_gate"
                for phase, target in _TRANSITIONS
                if f"{phase}→{target}" in APPROVAL_GATES
            }
        )
        gate_map["end"] = END
        workflow.add_node("gate_check", self._gate_check_node)
        workflow.add_conditional_edges("gate_check", self._gate_router, gate_map)

        # Approval gate node (calls interrupt)
        post_map: dict[Hashable, str] = {
            f"approved_{phase}→{target}": target
            for phase, target in _TRANSITIONS
            if f"{phase}→{target}" in APPROVAL_GATES
        }
        post_map["rejected"] = END
        workflow.add_node("approval_gate", self._approval_gate_node)
        workflow.add_conditional_edges("approval_gate", self._post_approval_router, post_map)

        return workflow.compile(checkpointer=self._make_checkpointer())

    def _router_node(self, state: PhaseState) -> PhaseState:
        """Route to the current phase from the state store.

        A phase value outside the lifecycle (corrupt state from before
        write-side validation) is normalized to the lifecycle start — the
        phase node then re-persists the repaired value.
        """
        current = self.state_store.get_current_phase()
        if current not in PHASE_ORDER:
            current = LIFECYCLE_START
        return PhaseState(
            current_phase=current,
            messages=state.messages,
            tool_calls=state.tool_calls,
            stop_reason=state.stop_reason,
        )

    def _route_to_current(self, state: PhaseState) -> str:
        """Conditional edge out of the router: stay where the store says."""
        return state.current_phase if state.current_phase in PHASE_ORDER else LIFECYCLE_START

    def _make_phase_node(self, phase: str) -> Any:
        """Create a node function for a phase."""

        def node(state: PhaseState) -> PhaseState:
            self.state_store.advance_phase(phase)
            return PhaseState(
                current_phase=phase,
                messages=state.messages,
                tool_calls=state.tool_calls,
                stop_reason=state.stop_reason,
            )

        return node

    def _gate_check_node(self, state: PhaseState) -> PhaseState:
        """Check if the current phase can advance."""
        return state

    def _gate_router(self, state: PhaseState) -> str:
        """Determine the next step based on gate conditions."""
        current = state.current_phase
        if current not in PHASE_ORDER:
            # Corrupt phase value — the router node already normalized it, so
            # the phase node re-persisted the repaired value. Stop the walk
            # rather than index into an unknown position.
            return "end"
        current_idx = PHASE_ORDER.index(current)
        # Last phase → end
        if current_idx >= len(PHASE_ORDER) - 1:
            return "end"

        next_phase = PHASE_ORDER[current_idx + 1]
        transition = f"{current}→{next_phase}"

        # Every gate requires its exit artifact before it passes — approval
        # only applies to the gated transitions.
        exit_digest = self.state_store.get_exit_artifact_digest(current)
        if exit_digest is None:
            # No exit artifact — can't advance, stay in phase
            return "end"

        if transition in APPROVAL_GATES:
            # Check if already approved with matching digest
            if self.state_store.is_approved(transition, exit_digest):
                return f"advance_{current}"

            # Need approval — route to approval gate
            return f"approve_{transition}"

        # Non-gated transition with exit artifact — advance directly
        return f"advance_{current}"

    def _approval_gate_node(self, state: PhaseState) -> PhaseState:
        """Call interrupt() to pause for human approval."""
        current = state.current_phase
        current_idx = PHASE_ORDER.index(current)
        next_phase = PHASE_ORDER[current_idx + 1]
        transition = f"{current}→{next_phase}"

        exit_digest = self.state_store.get_exit_artifact_digest(current) or ""

        # interrupt() pauses the graph and sends this data to the caller
        approval_request = {
            "transition": transition,
            "current_phase": current,
            "next_phase": next_phase,
            "artifact_digest": exit_digest,
            "message": f"Approve transition {transition}? (digest: {exit_digest[:8]})",
        }

        # This pauses execution — caller must resume with Command(resume=...)
        response = interrupt(approval_request)

        # When resumed, response contains the approval decision
        if isinstance(response, dict) and response.get("approved"):
            self.state_store.record_approval(transition, exit_digest)
            return PhaseState(
                current_phase=current,
                messages=state.messages,
                tool_calls=state.tool_calls,
                pending_approval=None,
            )
        else:
            return PhaseState(
                current_phase=current,
                messages=state.messages,
                tool_calls=state.tool_calls,
                pending_approval="rejected",
            )

    def _post_approval_router(self, state: PhaseState) -> str:
        """Route after approval gate."""
        if state.pending_approval == "rejected":
            return "rejected"
        current = state.current_phase
        current_idx = PHASE_ORDER.index(current)
        next_phase = PHASE_ORDER[current_idx + 1]
        transition = f"{current}→{next_phase}"
        return f"approved_{transition}"

    def run(
        self,
        thread_id: str = "",
        initial_messages: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Run the phase machine from the current phase.

        Runs under an explicit thread_id so a run that hits an approval
        interrupt can later be `resume(thread_id, ...)`'d. Defaults to the
        store's project scope so two projects' checkpoints never collide.
        """
        if not thread_id:
            thread_id = getattr(self.state_store, "project", "") or "default"
        state = PhaseState(
            current_phase=self.state_store.get_current_phase(),
            messages=initial_messages or [],
        )
        config = {"configurable": {"thread_id": thread_id}}
        result: dict[str, Any] = self.graph.invoke(state, config=config)
        return result

    def resume(self, thread_id: str, approval: dict[str, Any]) -> dict[str, Any]:
        """Resume a paused graph with an approval decision."""
        config = {"configurable": {"thread_id": thread_id}}
        result: dict[str, Any] = self.graph.invoke(Command(resume=approval), config=config)
        return result

    def check_gate(self, phase: str) -> dict[str, Any]:
        """Check gate status for a phase transition (non-blocking).

        Unknown phase values (corrupt store state) are reported, not raised —
        the gate endpoint is a read surface and must stay fail-closed.
        """
        if phase not in PHASE_ORDER:
            return {
                "phase": phase,
                "status": "invalid",
                "reason": f"unknown phase {phase!r}",
                "legal_phases": list(PHASE_ORDER),
            }
        current_idx = PHASE_ORDER.index(phase)
        if current_idx >= len(PHASE_ORDER) - 1:
            return {"phase": phase, "status": "terminal"}

        next_phase = PHASE_ORDER[current_idx + 1]
        transition = f"{phase}→{next_phase}"
        exit_digest = self.state_store.get_exit_artifact_digest(phase)

        result: dict[str, Any] = {
            "phase": phase,
            "transition": transition,
            "has_exit_artifact": exit_digest is not None,
            "requires_approval": transition in APPROVAL_GATES,
        }

        if exit_digest and transition in APPROVAL_GATES:
            result["approved"] = self.state_store.is_approved(transition, exit_digest)
            result["digest"] = exit_digest

        return result


def create_task_fanout(task_ids: list[str], target_node: str) -> list[Any]:
    """Create Send messages for parallel task execution.

    Used in plan/build/qa phases to fan out work items.
    """
    from langgraph.types import Send

    return [Send(target_node, {"task_id": tid}) for tid in task_ids]
