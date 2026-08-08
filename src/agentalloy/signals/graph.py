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

from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from agentalloy.code_index.slug import repo_slug
from agentalloy.storage.state_store import LEASED_KINDS, PhaseState
from agentalloy.storage.stream_id import resolve_stream_id

# Session-scoped delivery bookkeeping that is explicitly NOT graph state
# (approach.md §1; state_store.SESSION_SCOPED_KINDS). Pulling these into graph
# state would force ``thread_id = session`` — exactly the collapse #548 removes.
_NON_GRAPH_SESSION_KINDS: frozenset[str] = frozenset(
    {"announced", "composed", "banner-turns", "pause-reminded"}
)

# The four lanes (approach.md §1, §7 step 3 / coordination.md). ``sdd-full`` is
# the default linear sprint; the other three are the ``next_phase_hint`` routes.
_LANES: frozenset[str] = frozenset({"sdd-full", "sdd-fast", "add-skill", "flow"})


class PhaseGraphState(TypedDict, total=False):
    """Durable graph state (approach.md §1). Source of truth for the graph.

    Explicitly **excludes** session-scoped delivery bookkeeping
    (``announced``/``composed``/``banner-turns``/``pause-reminded``), which stay
    per-session on the existing store.
    """

    phase: str  # intake, spec, design, plan, build, qa, ship, sdd-fast, add-skill, flow
    lane: str  # sdd-full | sdd-fast | add-skill | flow
    paused: bool  # workflow pause flag (#550) — not a node
    paused_since: int | None  # (#550) epoch-ms
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
    mode = phase_state.mode if phase_state else None
    lane = mode if mode in _LANES else "sdd-full"
    paused = mode == "paused"
    return PhaseGraphState(
        phase=phase_state.phase if phase_state else "intake",
        lane=lane,
        paused=paused,
        paused_since=phase_state.paused_since if phase_state else None,
    )


__all__ = [
    "PhaseGraphState",
    "ThreadKey",
    "make_thread_key",
    "leased_graph_kinds",
    "initial_phase_graph_state",
    "to_phase_graph_state",
    "_LANES",
    "_NON_GRAPH_SESSION_KINDS",
]
