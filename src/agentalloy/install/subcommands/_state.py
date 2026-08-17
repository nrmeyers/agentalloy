# pyright: reportPrivateUsage=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
"""Shared state access for the phase-touching CLI surfaces.

Phase lives in the DuckDB ``sdd_state`` store and nowhere else — there is no
``.agentalloy/phase`` file to fall back to.  DuckDB is single-writer, so the
store handle belongs to the service; every other process reaches the same row
over HTTP.

Two callers, one seam:

* **In-process** (the service itself, and the test suite) — a store is bound via
  ``bind_process_store``, so use it directly.  Looping back over the HTTP API
  would be the service calling itself, and would deadlock on the writer lock.
* **Out-of-process** (an ordinary ``agentalloy phase set`` in a terminal) —
  nothing is bound, so :func:`require_service` produces a client for a
  *verified-running* service, or exits non-zero.

The fail-loud posture is the point of this module.  Every surface used to catch
``StateClientError`` and quietly write the file mirror instead, which meant a
service outage did not look like an outage: the CLI wrote one place, the service
read another, and the two drifted silently.  A down service now stops the
command rather than forking the truth.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from agentalloy.api.state_client import StateClient, StateClientError
from agentalloy.storage.state_store import PhaseState

# Fixed and greppable: docs, tests, and support answers all point at this text.
SERVICE_DOWN_MESSAGE = (
    "Error: the agentalloy service is not running, and SDD state lives only in "
    "the state store (there is no file fallback).\n"
    "  Start it with `agentalloy server-start`, then retry."
)

# How long to wait for a service we just asked to start.
_START_TIMEOUT_S = 15.0


def require_service(*, autostart: bool = True) -> StateClient:
    """A client for a running state service, or exit 1 with a fixed message.

    Tries, in order: an already-listening service; a background start; a bounded
    health poll.  *autostart* exists for callers that must not spawn anything
    (and for tests) — it never changes the outcome when the service is already
    up.
    """
    client = StateClient()
    if client.is_running():
        return client

    if autostart and _try_start():
        deadline = time.monotonic() + _START_TIMEOUT_S
        while time.monotonic() < deadline:
            if client.is_running():
                return client
            time.sleep(0.25)

    print(SERVICE_DOWN_MESSAGE, file=sys.stderr)
    raise SystemExit(1)


def _try_start() -> bool:
    """Launch the service in the background.  False when the launch itself failed.

    A False here is not the failure the caller reports — the health poll is what
    decides.  This only says whether it was worth waiting.
    """
    try:
        from agentalloy.install import server_proc

        server_proc.start_background(server_proc.configured_port())
        return True
    except Exception:  # noqa: BLE001 — a failed start is reported by the poll below
        return False


# ---------------------------------------------------------------------------
# Phase access
# ---------------------------------------------------------------------------


class PhaseAccess(Protocol):
    """The three phase operations every CLI surface needs, transport-agnostic."""

    def read(self) -> PhaseState | None:
        """The decoded phase row, or ``None`` when the repo has no phase.

        ``None`` means *recorded absence*.  An unreachable store raises instead,
        so "no phase" can never be an outage in disguise.
        """
        ...

    def write(
        self,
        phase: str,
        *,
        actor: str | None = None,
        mode: str | None = None,
        paused_since: str | None = None,
        override: bool = False,
    ) -> dict[str, Any] | None:
        """Write *phase*, carrying forward any field left as ``None``.

        ``mode=""``/``paused_since=""`` clear those fields; that asymmetry is the
        store's, and it is what lets ``workflow resume`` drop pause without
        touching the phase.  Returns the service response body (for gate verdict
        visibility) or ``None`` when the store has no return type.
        """
        ...

    def clear(self) -> None:
        """Delete the phase row.  Idempotent."""
        ...

    def contracts_handle(self) -> Any:
        """The object the gate predicates query contracts through.

        ``PredicateContext.store`` only ever calls
        ``list_contracts(phase=, slug=, status=)``, which the store and the HTTP
        client both provide.  It used to be ``None`` whenever the service was
        down, so the two contract predicates evaluated UNKNOWN and failed open —
        the design→build coverage gate was vacuous for every CLI ``phase set``
        made without a service.  There is no such case left: a phase set that
        gets this far has a reachable store.
        """
        ...

    def session_handle(self) -> Any:
        """The object for session management (list, archive, etc.)."""
        ...


@dataclass(frozen=True)
class _StoreAccess:
    """Phase access through the in-process store handle."""

    store: Any

    def read(self) -> PhaseState | None:
        return self.store.read_phase()

    def write(
        self,
        phase: str,
        *,
        actor: str | None = None,
        mode: str | None = None,
        paused_since: str | None = None,
        override: bool = False,
    ) -> None:
        self.store.write_phase(phase, actor=actor, mode=mode, paused_since=paused_since)

    def clear(self) -> None:
        self.store.clear("phase")

    def contracts_handle(self) -> Any:
        return self.store

    def session_handle(self) -> Any:
        return self.store


@dataclass(frozen=True)
class _ServiceAccess:
    """Phase access over HTTP, for a CLI running outside the service."""

    client: StateClient
    root: Path

    def read(self) -> PhaseState | None:
        body = self.client.get_phase(repo_root=str(self.root))
        if body is None:
            return None
        phase = str(body["value"])
        return PhaseState(
            phase=phase,
            mode=body.get("mode"),
            paused_since=body.get("paused_since"),
            transitioned_by=body.get("transitioned_by"),
            started_at=body.get("started_at"),
            last_updated=body.get("last_updated"),
            phase_start_ref=body.get("phase_start_ref"),
            # Derived, not trusted from the wire — same rule the store applies.
            workflow=f"sdd-{phase}",
        )

    def write(
        self,
        phase: str,
        *,
        actor: str | None = None,
        mode: str | None = None,
        paused_since: str | None = None,
        override: bool = False,
    ) -> dict[str, Any] | None:
        """Advance the phase over HTTP and return the service's response body.

        The body carries ``gate_verdict`` when the service re-evaluated and
        declined to write (the CLI already gate-checks locally, but the two
        must agree — when they don't, the caller must see the verdict, not a
        silent no-op) (#501).
        """
        return self.client.set_phase(
            phase,
            repo_root=str(self.root),
            actor=actor,
            mode=mode,
            paused_since=paused_since,
            override=override,
        )

    def clear(self) -> None:
        self.client.clear_phase(repo_root=str(self.root))

    def contracts_handle(self) -> Any:
        return self.client

    def session_handle(self) -> Any:
        return self.client


def phase_access(root: Path, *, autostart: bool = True) -> PhaseAccess:
    """Phase access for *root*: the bound store if there is one, else the service."""
    from agentalloy.storage.state_store import process_store

    store = process_store()
    if store is not None:
        from agentalloy.api.state_router import scoped_state_store  # noqa: PLC0415

        return _StoreAccess(scoped_state_store(store, root))
    return _ServiceAccess(require_service(autostart=autostart), root)


def fail_on_state_error(exc: StateClientError) -> None:
    """Report a service-side failure and exit non-zero.

    The old handler printed a warning and wrote the file mirror.  A 500 from the
    service is a reason to stop, not a reason to write somewhere else.
    """
    detail = f" (HTTP {exc.status})" if exc.status is not None else ""
    print(f"Error: the agentalloy service failed{detail}: {exc.message}", file=sys.stderr)
    raise SystemExit(1)
