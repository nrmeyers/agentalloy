# pyright: reportPrivateUsage=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
"""SDD state and contract endpoints — GET/POST /state/* and /contracts routes.

Exposes the DuckDB-backed SDD state store over HTTP so out-of-process callers
(CLI subcommands, web UI) can read and mutate phase, cursor, and approval
state through the service.

In-process callers inside the service (compose, proxy_signal, signals) hold
the StateStore directly and do **not** make an HTTP call.  Only out-of-process
callers go over HTTP.  This split avoids a network hop on the hot compose path
while keeping a single source of truth for state mutations.

The store is opened once during the app lifespan and injected into routes via
the :func:`get_state_store` dependency.  Routes never open the database
themselves.
"""

from __future__ import annotations

import asyncio
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from agentalloy.api.state_models import (
    ALL_KINDS,
    ArtifactListResponse,
    ArtifactResponse,
    ArtifactSetRequest,
    ContractCreateRequest,
    ContractListResponse,
    ContractPatchRequest,
    ContractResponse,
    ContractSupersedeRequest,
    PhaseAdvanceRequest,
    PhaseAdvanceResponse,
    PhaseReadResponse,
    ResumeContractInfo,
    ResumeResponse,
    StateAllResponse,
    StateConflictInfo,
    StateReadResponse,
    StateWriteRequest,
    StateWriteResponse,
)
from agentalloy.code_index.slug import repo_slug
from agentalloy.providers.base import end_session_instruction
from agentalloy.storage.state_store import DuckDBStateStore, StateStoreError
from agentalloy.storage.stream_id import resolve_stream_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/state", tags=["state"])
contract_router = APIRouter(prefix="/contracts", tags=["contracts"])


# Fire-and-forget compose tasks. The event loop holds only a weak reference to
# a Task, so an unretained ``asyncio.create_task`` can be garbage-collected
# mid-flight (the compose never runs). Keep strong references here until each
# task completes.
_background_tasks: set[asyncio.Task[None]] = set()


def _spawn_background(coro: Any) -> None:
    """Schedule *coro* as a fire-and-forget background task, retaining a strong
    reference so it cannot be GC'd before it completes."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


# ---------------------------------------------------------------------------
# Dependency providers — overridden during the app lifespan (or by tests).
# ---------------------------------------------------------------------------


def get_state_store() -> DuckDBStateStore:
    """Return the lifespan-scoped StateStore.

    Raises :class:`RuntimeError` if called before the lifespan has wired the
    store (e.g. a bare router without a running app).
    """
    raise RuntimeError("get_state_store must be bound during app lifespan")


_REPO_ROOT_QUERY = Query(
    default=None,
    description=(
        "Absolute path to the repository root.  Determines which repo's state "
        "the call reads or writes.  Omitted means the repo the service was "
        "deployed against (AGENTALLOY_PROJECT_DIR, else the process cwd)."
    ),
)


def default_repo_root() -> Path:
    """The repo a request belongs to when it names none.

    Mirrors :func:`agentalloy.api.proxy_context.resolve_working_dir` minus the
    proxy-only sources, so both seams answer "which repo is this?" the same way.
    """
    env_dir = os.environ.get("AGENTALLOY_PROJECT_DIR")
    if env_dir:
        return Path(env_dir)
    return Path.cwd()


def resolve_repo_root(repo_root: str | None = _REPO_ROOT_QUERY) -> Path:
    """Resolve the repo root for this request."""
    return Path(repo_root) if repo_root else default_repo_root()


@lru_cache(maxsize=256)
def _repo_key_for(root: str) -> str:
    """Slug a repo root path via git origin remote.

    Deliberately worktree-collapsing (see ``repo_slug``) — code-index lookups
    and phase/state rows share this key, so every worktree of a repo resolves
    to the same ``repo``. Worktree isolation lives in ``stream_id`` instead.
    """
    return repo_slug(Path(root))


def _stream_key_for(root: str) -> str:
    """Resolve the per-worktree stream key for a repo root path.

    Distinguishes concurrent worktrees of the same repo, which ``repo_slug``
    deliberately does not (issue #548). Not cached, unlike ``_repo_key_for``:
    ``resolve_stream_id`` is a cheap file read/hash (no external process), and
    ``.agentalloy/.stream`` is meant to be rebindable at runtime via
    ``agentalloy stream use`` — caching it would freeze the old binding until
    process restart.
    """
    return resolve_stream_id(Path(root))


def scoped_state_store(store: DuckDBStateStore, root: Path) -> DuckDBStateStore:
    """The store view for *root*'s workflow state: ``(repo_slug, stream_id)``.

    Use for phase/workflow reads and writes — anywhere a worktree's own phase
    must not be visible to (or clobbered by) a sibling worktree of the same
    repo. Code-index-adjacent artifact storage (design docs, lessons, ingest
    bookkeeping) is deliberately repo-only: it does not call this helper.
    """
    root_s = str(root)
    return store.for_repo(_repo_key_for(root_s), stream_id=_stream_key_for(root_s))


def get_repo_store(
    root: Path = Depends(resolve_repo_root),
    store: DuckDBStateStore = Depends(get_state_store),
) -> DuckDBStateStore:
    """The lifespan store, scoped to the repo and stream this request is about.

    One service serves every repo from one ``state.duck``; without this scoping
    they all share a single bucket and a phase set in one repo is read by the
    next.  ``repo`` collapses worktrees (matches the code index); ``stream_id``
    re-splits them so concurrent worktrees of the same repo don't cross phases.
    """
    return scoped_state_store(store, root)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_result_to_response(
    result: Any,
) -> tuple[int, StateWriteResponse | StateConflictInfo]:
    """Convert a StateWriteResult into an HTTP status + response model."""
    conflict = result.conflict
    # conflict.owner is None when no row exists yet (acquire_lease refuses
    # to create ghost rows).  There is nothing to conflict with — treat as
    # non-blocking so the write proceeds.
    if conflict is not None and conflict.owner is not None:
        return 409, StateConflictInfo(
            owner=conflict.owner,
            lease_expires_at=conflict.lease_expires_at,
            message=conflict.message,
        )
    return 200, StateWriteResponse(
        kind=result.kind,
        value=result.value,
        owner=result.owner,
        lease_expires_at=result.lease_expires_at,
    )


#: Gate predicates that name a file the phase owes.  Others (`approval_recorded`,
#: `build_contracts_cover_tasks`) gate on state, not on a path, and have nothing
#: to list here.
_ARTIFACT_PREDICATES = ("artifact_exists", "artifact_contains")


def _stamp_phase_start_ref(store: DuckDBStateStore, project_root: Path | None) -> None:
    """Stamp the current HEAD SHA as the phase-start ref on a real transition.

    Called from the HTTP API so that every real phase transition (whether
    initiated via the CLI or the HTTP API) records the entry-point HEAD,
    which the ``scope_touched_in_diff`` predicate later reads.
    """
    try:
        from agentalloy.signals.skill_loader import _record_phase_start_ref

        # Use the passed project_root — it's the canonical repo root the caller
        # resolved. The store handle already comes from ``get_repo_store`` which
        # scopes to the same repo.
        _record_phase_start_ref(project_root or Path.cwd())
    except Exception:  # noqa: BLE001 — soft; the phase advance must not fail
        pass


def _owed_artifacts(gates: dict[str, Any]) -> list[str]:
    """The distinct artifact paths a phase's exit gates require.

    Exit gates are a boolean tree: ``{"all_of": [{predicate: {...}}, ...]}``.
    This walks it for the path-bearing predicates.  The previous version
    iterated ``gates.items()`` expecting ``{gate_name: {"artifact": path}}`` —
    a shape the corpus has never emitted — so ``owed_artifacts`` was silently
    always empty, and the ``except`` around it meant nothing ever said so.
    """
    paths: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:  # pyright: ignore[reportUnknownVariableType]
                walk(item)
            return
        if not isinstance(node, dict):
            return
        for key, spec in node.items():  # pyright: ignore[reportUnknownVariableType]
            if key in _ARTIFACT_PREDICATES and isinstance(spec, dict):
                path = spec.get("path")  # pyright: ignore[reportUnknownMemberType]
                if isinstance(path, str) and path not in paths:
                    paths.append(path)
            else:
                walk(spec)

    walk(gates)
    return paths


def _contract_row_to_response(row: dict[str, Any]) -> ContractResponse:
    """Convert a store contract dict to a ContractResponse model."""
    return ContractResponse(
        contract_id=row["contract_id"],
        phase=row["phase"],
        slug=row["slug"],
        work_item=row.get("work_item"),
        route=row.get("route"),
        domain_tags=row.get("domain_tags"),
        scope_touches=row.get("scope_touches"),
        scope_avoids=row.get("scope_avoids"),
        success_criteria=row.get("success_criteria"),
        status=row["status"],
        supersedes=row.get("supersedes"),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        body=row.get("body"),
    )


def _rewrite_posture(root: Path, phase: str, mode: str | None) -> list[str]:
    """Rewrite enforcement posture files for wired Tier A harnesses.

    Thin wrapper around ``rewrite_enforcement_posture`` so the router can
    call it from ``asyncio.to_thread``.  Soft: any failure is logged and
    swallowed — a posture rewrite failure must not block the phase advance.

    ``mode`` is passed explicitly by the caller (the just-committed
    ``PhaseState.mode``, read off the same in-process store handle that did
    the write) rather than re-derived here — one evaluation point for the
    ``(phase, mode)`` pair, reachable identically from the service and the
    CLI (see ``install.subcommands.workflow``).
    """
    try:
        from agentalloy.install.subcommands.wire_harness import (
            rewrite_enforcement_posture as _rewrite,
        )

        return _rewrite(root, phase, mode=mode)
    except Exception:
        logger.debug(
            "posture rewrite failed for %s phase=%s mode=%s",
            root,
            phase,
            mode,
            exc_info=True,
        )
        return []


async def _trigger_compose_in_process(
    store: DuckDBStateStore,
    contract_id: str,
    request: Request,
) -> None:
    """Trigger compose in-process after a successful contract write.

    Retrieves the ComposeOrchestrator from app.state and composes using the
    stored contract's phase, domain_tags, and body.  Runs asynchronously so
    it does not block the HTTP response.

    The task is stored on ``request.app.state`` to prevent garbage collection
    and to surface failures at WARNING level (A4: the compose trigger must be
    observable — a silently dropped trigger defeats acceptance criterion B3).
    """
    orchestrator = getattr(request.app.state, "compose_orchestrator", None)
    if orchestrator is None:
        logger.warning(
            "compose_orchestrator not available on app.state — "
            "in-process compose will not run for contract %s",
            contract_id,
        )
        return

    contract = store.get_contract(contract_id)
    if contract is None:
        logger.warning("Contract %s not found for compose trigger", contract_id)
        return

    from agentalloy.api.compose_models import ComposeRequest

    compose_req = ComposeRequest(
        task=contract.get("body") or contract.get("slug", ""),
        phase=contract["phase"],  # type: ignore[arg-type]
        contract_tags=contract.get("domain_tags"),
        requesting_agent="contract_write",
        legs="both",
    )

    async def _run() -> None:
        try:
            await orchestrator.compose(compose_req)
        except Exception:
            logger.exception("In-process compose failed for contract %s", contract_id)

        # 4. Gate feedback: evaluate AC completeness after compose and store
        #     feedback as a gate_feedback artifact.
        try:
            from agentalloy.signals.predicates import (
                _evaluate_ac_feedback,
                _gate_trigger_enabled,
            )

            if _gate_trigger_enabled():
                feedback = _evaluate_ac_feedback(store, contract)
                if feedback is not None:
                    store.set_artifact(
                        contract["phase"],
                        contract["slug"],
                        "gate_feedback",
                        feedback,
                    )
        except Exception:
            logger.debug(
                "Gate feedback evaluation failed for contract %s",
                contract_id,
                exc_info=True,
            )

    task = asyncio.create_task(_run())
    # Prevent GC: store the task on app.state keyed by contract_id.
    # The task self-removes on completion via the done callback.
    state = request.app.state
    tasks = getattr(state, "compose_tasks", None)
    if tasks is None:
        tasks = {}
        state.compose_tasks = tasks

    def _compose_done_cb(_t: object) -> None:
        tasks.pop(contract_id, None)

    task.add_done_callback(_compose_done_cb)
    tasks[contract_id] = task


# ---------------------------------------------------------------------------
# GET /state — all kinds
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=StateAllResponse,
    summary="Read all state kinds for the resolved repo",
)
async def read_all_state(
    store: DuckDBStateStore = Depends(get_repo_store),
) -> StateAllResponse:
    """Every kind that has a value, with ``phase`` unwrapped to its bare name.

    ``phase`` is stored as a blob; its raw row is JSON that no consumer of this
    map is prepared to parse.  Unwrapping happens here so that nothing outside
    the store ever handles the blob.
    """
    state: dict[str, str] = {}
    for kind in sorted(ALL_KINDS):
        if kind == "phase":
            phase = await asyncio.to_thread(store.read_phase)
            if phase is not None:
                state["phase"] = phase.phase
            continue
        value = await asyncio.to_thread(store.read, kind)
        if value is not None:
            state[kind] = value
    return StateAllResponse(state=state)


# ---------------------------------------------------------------------------
# GET /state/resume — assembled cold-session bootstrap (before /{kind})
# ---------------------------------------------------------------------------


@router.get(
    "/resume",
    response_model=ResumeResponse,
    summary="Assembled resume data for cold-session bootstrap",
)
async def read_resume(
    store: DuckDBStateStore = Depends(get_repo_store),
) -> ResumeResponse:
    """Assemble phase, cursor'd work-item, owed artifacts, and governing decisions.

    This is a single server-side endpoint that replaces the client-side
    fan-out of four separate calls.  The shape matches what the proxy's
    orientation block sends to the agent.
    """
    phase_state = await asyncio.to_thread(store.read_phase)
    phase = phase_state.phase if phase_state is not None else None
    cursor_value = await asyncio.to_thread(store.read, "cursor")

    cursor_contract: ResumeContractInfo | None = None
    owed_artifacts: list[str] | None = None
    governing_decisions: list[str] | None = None

    if cursor_value:
        # Extract contract_id from cursor value (format: "active/<phase>/<slug>.md")
        parts = cursor_value.strip().rsplit("/", 1)
        if len(parts) == 2:
            slug_file = parts[1]
            slug = slug_file.rsplit(".md", 1)[0] if slug_file.endswith(".md") else parts[1]

            # Find the contract by slug
            contracts = await asyncio.to_thread(store.list_contracts, slug=slug, status="active")
            if contracts:
                row = contracts[0]
                cursor_contract = ResumeContractInfo(
                    contract_id=row["contract_id"],
                    phase=row["phase"],
                    slug=row["slug"],
                    domain_tags=row.get("domain_tags"),
                    scope_touches=row.get("scope_touches"),
                    scope_avoids=row.get("scope_avoids"),
                    body=row.get("body"),
                )

    if phase:
        try:
            from agentalloy.signals.skill_loader import exit_gates_for_phase

            owed_artifacts = _owed_artifacts(exit_gates_for_phase(phase) or {}) or None
        except Exception:
            logger.debug("exit gates unavailable for phase=%s", phase, exc_info=True)

    return ResumeResponse(
        phase=phase,
        cursor_contract=cursor_contract,
        owed_artifacts=owed_artifacts,
        governing_decisions=governing_decisions,
    )


# ---------------------------------------------------------------------------
# GET /state/phase — the bare phase name (before /{kind})
# ---------------------------------------------------------------------------


@router.get(
    "/phase",
    response_model=PhaseReadResponse,
    summary="Read the current phase, decoded",
)
async def read_phase(
    store: DuckDBStateStore = Depends(get_repo_store),
) -> PhaseReadResponse:
    """Return the decoded phase row — never the stored blob verbatim.

    Its own route rather than a branch inside ``/{kind}`` because the two
    return genuinely different things: every other kind's stored value *is*
    its value, while ``phase``'s row is JSON carrying mode, actor, and
    timestamps alongside the name.  Serving it through the generic route
    handed every caller a JSON envelope where it expected ``"build"``.

    ``value`` stays the bare name so that older callers keep working; the
    decoded fields ride alongside it because the CLI reads the whole row and,
    without them, had no way to reach ``mode``/``paused_since``/``transitioned_by``
    except the file mirror this migration is removing.

    Note the deliberate asymmetry with writes: ``POST /state/phase`` is gated
    (task 09), this read is not.  Reading a phase cannot advance one.
    """
    phase = await asyncio.to_thread(store.read_phase)
    if phase is None:
        return PhaseReadResponse(kind="phase", value=None)
    return PhaseReadResponse(
        kind="phase",
        value=phase.phase,
        mode=phase.mode,
        paused_since=phase.paused_since,
        transitioned_by=phase.transitioned_by,
        started_at=phase.started_at,
        last_updated=phase.last_updated,
        workflow=phase.workflow or None,
        phase_start_ref=phase.phase_start_ref,
    )


@router.delete(
    "/phase",
    response_model=StateReadResponse,
    summary="Clear the current phase",
)
async def clear_phase(
    store: DuckDBStateStore = Depends(get_repo_store),
) -> StateReadResponse:
    """Delete the phase row, leaving the repo genuinely phase-less.

    ``agentalloy phase clear`` used to ``unlink()`` the file; with the store as
    the only source it needs a route, and it must *delete* rather than write an
    empty value — a row holding ``""`` still reads as present to every consumer
    that checks for ``None``.  Idempotent: clearing an absent phase is a
    success, so reset paths stay re-runnable.
    """
    await asyncio.to_thread(store.clear, "phase")
    return StateReadResponse(kind="phase", value=None)


# ---------------------------------------------------------------------------
# GET /state/{kind} — single kind
# ---------------------------------------------------------------------------


@router.delete(
    "/repo",
    summary="Drop all state + contract rows for a repo",
)
async def delete_repo(
    root: Path = Depends(resolve_repo_root),
    store: DuckDBStateStore = Depends(get_state_store),
) -> dict[str, int]:
    """Delete every ``sdd_state``/``sdd_contract`` row keyed to this repo.

    Keys on the same canonical slug ``get_repo_store`` uses
    (:func:`_repo_key_for`), not the raw ``repo_root`` path — rows are written
    under the slug, so deleting by path silently matches nothing. This is the
    HTTP counterpart to the direct-store branch in ``uninstall._unwire_repo_local``,
    for callers (e.g. ``agentalloy unwire``) that cannot safely open a second
    DuckDB writer while the service holds the file.
    """
    deleted = await asyncio.to_thread(store.delete_repo_rows, _repo_key_for(str(root)))
    return {"deleted_rows": deleted}


@router.get(
    "/artifact",
    response_model=ArtifactListResponse,
    summary="List deliverable artifacts for a phase",
)
async def list_artifacts(
    phase: str = Query(description="Phase to list artifacts for"),
    slug: str | None = Query(default=None, description="Filter by slug"),
    name_glob: str | None = Query(default=None, description="fnmatch pattern over name"),
    store: DuckDBStateStore = Depends(get_repo_store),
) -> ArtifactListResponse:
    rows: list[dict[str, Any]] = await asyncio.to_thread(
        store.list_artifacts,
        phase,
        slug=slug,
        name_glob=name_glob,
    )
    # Convert datetime.updated_at to ISO string for pydantic validation
    cleaned: list[dict[str, Any]] = []
    for row in rows:
        row = dict(row)  # sqlite3.Row → mutable dict
        ua = row.get("updated_at")
        if ua is None:
            continue
        if hasattr(ua, "isoformat"):
            row = {**row, "updated_at": ua.isoformat()}
        cleaned.append(row)
    return ArtifactListResponse(artifacts=[ArtifactResponse(**row) for row in cleaned])


@router.get(
    "/artifact/{phase}/{slug}/{name}",
    response_model=ArtifactResponse,
    responses={404: {"description": "Artifact not found"}},
    summary="Get a single artifact by (phase, slug, name)",
)
async def get_artifact(
    phase: str,
    slug: str,
    name: str,
    store: DuckDBStateStore = Depends(get_repo_store),
) -> ArtifactResponse:
    """Fetch a single artifact by (phase, slug, name).

    Returns 404 when the artifact does not exist.  Filters to
    ``status='active'`` by default so the route is lifecycle-ready
    from issue #520 without a follow-up change.
    """
    row = await asyncio.to_thread(store.get_artifact, phase, slug, name, status="active")
    if row is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    # Convert datetime.updated_at to ISO string for pydantic validation
    if hasattr(row.get("updated_at"), "isoformat"):
        row = {**row, "updated_at": row["updated_at"].isoformat()}
    # Strip 'status' — it's returned by the store but not in ArtifactResponse
    cleaned = {k: v for k, v in row.items() if k in ArtifactResponse.model_fields}
    return ArtifactResponse(**cleaned)


@router.get(
    "/sessions/active",
    summary="List active sessions for this repo+stream",
)
async def list_active_sessions(
    store: DuckDBStateStore = Depends(get_repo_store),
) -> list[dict[str, Any]]:
    """List all active sessions for this repo+stream, ordered by last_active_at desc."""
    return await asyncio.to_thread(store.list_active_sessions)


@router.post(
    "/sessions/archive",
    summary="Archive a session by session_key",
)
async def archive_session(
    body: dict[str, Any],
    store: DuckDBStateStore = Depends(get_repo_store),
) -> dict[str, bool]:
    """Archive a session by session_key. Returns {"archived": bool}."""
    session_key = body.get("session_key")
    if not session_key:
        raise HTTPException(status_code=400, detail="session_key is required")
    archived = await asyncio.to_thread(store.archive_session, session_key)
    return {"archived": archived}


@router.post(
    "/sessions/resume",
    summary="Re-activate a session by session_key",
)
async def resume_session(
    body: dict[str, Any],
    store: DuckDBStateStore = Depends(get_repo_store),
) -> dict[str, bool]:
    """Re-activate a session (archived → active) and refresh last_active_at.

    Returns ``{"resumed": bool}`` — True when the session is known (active or
    archived), False when no such session exists for this repo+stream.
    """
    session_key = body.get("session_key")
    if not session_key:
        raise HTTPException(status_code=400, detail="session_key is required")
    resumed = await asyncio.to_thread(store.resume_session, session_key)
    return {"resumed": resumed}


@router.post(
    "/archive-all",
    summary="Archive all active contracts and artifacts",
)
async def archive_all(
    store: DuckDBStateStore = Depends(get_repo_store),
) -> dict[str, int]:
    """Archive every active contract and artifact in one transaction.

    Returns ``{"contracts_archived": int, "artifacts_archived": int}``.
    Zero counts is a valid no-op (everything already archived) — not an error.
    """
    return await asyncio.to_thread(store.archive_all)


@router.get(
    "/{kind}",
    response_model=StateReadResponse,
    responses={
        404: {"description": "Kind not recognized"},
    },
    summary="Read a single state kind",
)
async def read_state(
    kind: str,
    session_key: str | None = Query(
        default=None,
        description="Session-scoped kinds (and phase-scoped 'approved') use this.",
    ),
    store: DuckDBStateStore = Depends(get_repo_store),
) -> StateReadResponse:
    """Read one kind's stored value verbatim.

    ``phase`` is excluded: its row is a blob, and this route's contract is
    that ``value`` is the value.  FastAPI already routes ``/state/phase`` to
    the specific handler above, so the guard only fires on a request that
    reached here some other way — it exists so the exclusion is enforced by
    the code rather than by route-declaration order.
    """
    if kind == "phase":  # pragma: no cover — unreachable while /phase is declared first
        raise HTTPException(
            status_code=404,
            detail="read 'phase' via GET /state/phase; its stored row is a blob",
        )
    if kind not in ALL_KINDS:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown state kind {kind!r}; expected one of {sorted(ALL_KINDS)}",
        )
    value = await asyncio.to_thread(store.read, kind, session_key)
    return StateReadResponse(kind=kind, value=value)


# ---------------------------------------------------------------------------
# POST /state/phase — with optional contract (transactional)
# ---------------------------------------------------------------------------


def _route_phase(
    store: DuckDBStateStore,
    current_phase: str | None,
    lane: str = "sdd-full",
    target_phase: str | None = None,
    override: bool = False,
    project_root: Path | None = None,
) -> dict[str, Any] | None:
    """Route a phase transition through the LangGraph.

    Uses the graph's ``_route_step`` which wraps ``evaluate_phase_gate`` —
    the single decision point for all routing (proxy, CLI, API).

    When ``target_phase`` is provided, it overrides the automatic
    ``_PHASE_GRAPH`` lookup so the caller can evaluate a specific
    transition (used by the HTTP / CLI path).
    """
    from agentalloy.signals.graph import _route_step  # noqa: PLC0415

    out = _route_step(
        current_phase or "",
        lane,
        target_phase=target_phase,
        override=override,
        project_root=project_root,
        store=store,
    )
    # _route_step always returns RoutingOutcome; same-phase no-op returns
    # to_phase==from_phase so we skip the gate here.
    if out.to_phase == (current_phase or ""):
        return None
    result: dict[str, Any] = {
        "should_transition": out.should_transition,
        "to_phase": out.to_phase,
        "advisories": list(out.advisories),
        "lane": out.lane,
    }
    # Carry through the verdict reason so callers can distinguish
    # "approval" vs "not_met" without re-evaluating the gate.
    if out.reason:
        result["reason"] = out.reason
    return result


@router.post(
    "/phase",
    response_model=PhaseAdvanceResponse,
    responses={
        409: {"model": StateConflictInfo, "description": "Lease conflict"},
    },
    summary="Set the current SDD phase (with optional contract)",
)
async def write_phase(
    req: PhaseAdvanceRequest,
    request: Request,
    repo_root: str | None = Query(
        default=None,
        description=(
            "Absolute path to the repository root.  When provided the endpoint "
            "rewrites the enforcement posture files for wired Tier A harnesses "
            "after a successful phase advance."
        ),
    ),
    store: DuckDBStateStore = Depends(get_repo_store),
) -> PhaseAdvanceResponse:
    """Advance the phase, optionally storing a contract in the same transaction.

    When ``req.contract`` is provided, both the phase write and the contract
    write commit together.  If the contract payload fails validation or the
    contract write fails, the phase advance is rolled back entirely.

    On success, compose is triggered in-process (the write *is* the trigger).
    When ``repo_root`` is provided the enforcement posture is rewritten for
    any wired Tier A harnesses (D1–D9).

    **Exit gate:** this endpoint evaluates the exit gate for the transition.
    The gate is evaluated *before* the write.  If it blocks, the verdict is
    returned in ``gate_verdict`` and the write is skipped (the CLI renders
    the blocking reason and missing-artifact paths).  Override bypasses the
    gate except for always-approval phases.

    Both paths go through ``store.write_phase`` rather than a raw
    ``write("phase", ...)``: a raw write replaces the blob with a bare name and
    so silently discards ``mode``, ``paused_since``, ``started_at`` and
    ``transitioned_by``.  ``read_phase`` tolerates that shape, which is exactly
    why the loss was invisible.
    """
    phase_value = req.value
    # ``actor`` is who *moved* the phase; ``owner`` is who holds the lease.  They
    # are usually the same session, and were conflated while only the lease had a
    # field — falling back keeps every existing caller behaving identically.
    actor = req.actor or req.owner

    # --- Exit gate evaluation (slice 09) ---
    current_phase_row = store.read_phase()
    current_phase = current_phase_row.phase if current_phase_row else None
    project_root = Path(repo_root) if repo_root else None
    # Route through the graph's route_step (single decision point).
    # Runs in a worker thread: the gate does file I/O + embedding, which must
    # not block the event loop (mirrors the store.write_phase call below).
    verdict = await asyncio.to_thread(
        _route_phase,
        store,
        current_phase,
        lane="sdd-full",  # HTTP path doesn't carry a lane; target_phase overrides _PHASE_GRAPH lookup
        target_phase=phase_value,
        override=req.override,
        project_root=project_root,
    )
    # A same-phase no-op routes to the current phase, so _route_phase returns
    # None. Only a real transition (verdict is not None) should stamp the
    # phase-start HEAD ref — re-stamping on a no-op would push the entry ref
    # forward and shrink the scope_touched_in_diff window.
    is_real_transition = verdict is not None
    if verdict is not None and not verdict.get("should_transition", True):
        # Gate blocks the transition — return verdict without writing. Mark
        # success=False so a 2xx cannot be read as a recorded advance (#501).
        return PhaseAdvanceResponse(
            kind="phase",
            value=phase_value,
            gate_verdict=verdict,
            success=False,
        )

    # Fast path: no contract — use the existing non-transactional path.
    # The ``phase`` row is the authoritative source (retired the graph
    # checkpoint shim in step 08).
    if req.contract is None:
        result = await asyncio.to_thread(
            store.write_phase,
            phase_value,
            actor=actor,
            owner=req.owner,
            mode=req.mode,
            paused_since=req.paused_since,
        )
        http_status, response = _write_result_to_response(result)
        if http_status != 200:
            raise HTTPException(
                status_code=409,
                detail=response.model_dump(mode="json"),  # type: ignore[union-attr]
            )
        # Posture rewrite after successful phase advance. Read the row back
        # off the same in-process store handle that just wrote it, rather
        # than re-deriving mode from ``req.mode`` (usually ``None`` — a phase
        # advance normally doesn't touch mode, which carries forward) or
        # trusting a second read through a different seam.
        if repo_root is not None:
            written = await asyncio.to_thread(store.read_phase)
            await asyncio.to_thread(
                _rewrite_posture,
                Path(repo_root),
                phase_value,
                written.mode if written else None,
            )
        # Stamp the phase-start HEAD ref on a real transition (soft — must not
        # block the phase advance). Skipped on a same-phase no-op so the entry
        # ref is not pushed forward.
        if is_real_transition:
            _stamp_phase_start_ref(store, project_root)
        return PhaseAdvanceResponse(
            kind=result.kind,
            # ``result.value`` is the stored blob; callers want the bare name.
            value=phase_value,
            owner=result.owner,
            lease_expires_at=result.lease_expires_at,
            end_session_instruction=end_session_instruction(phase_value),
            gate_verdict=None,
        )

    # Transactional path: phase + contract in one BEGIN/COMMIT
    contract_id: str | None = None
    written_mode: str | None = None
    try:
        with store.transaction() as tx:
            # Write the phase — blob semantics, reusing this transaction.
            result = tx.write_phase(
                phase_value,
                actor=actor,
                owner=req.owner,
                mode=req.mode,
                paused_since=req.paused_since,
            )
            # conflict.owner is None when no row exists yet — non-blocking
            if result.conflict is not None and result.conflict.owner is not None:
                # lease_expires_at is also non-None when owner is non-None
                assert result.conflict.lease_expires_at is not None
                raise HTTPException(
                    status_code=409,
                    detail=StateConflictInfo(
                        owner=result.conflict.owner,
                        lease_expires_at=result.conflict.lease_expires_at,
                        message=result.conflict.message,
                    ).model_dump(mode="json"),
                )

            # Read the mode back inside the same transaction (read-your-own-write)
            # so the posture rewrite below gets the resolved value, not a guess.
            written_phase = tx.read_phase()
            written_mode = written_phase.mode if written_phase else None

            # Write the contract
            contract = req.contract
            contract_id = tx.put_contract(
                contract.contract_id,
                phase=contract.phase,
                slug=contract.slug,
                work_item=contract.work_item,
                route=contract.route,
                domain_tags=contract.domain_tags,
                scope_touches=contract.scope_touches,
                scope_avoids=contract.scope_avoids,
                success_criteria=contract.success_criteria,
                body=contract.body,
            )
        # Transaction committed — posture rewrite + in-process compose.
        if repo_root is not None:
            await asyncio.to_thread(_rewrite_posture, Path(repo_root), phase_value, written_mode)
        # Stamp the phase-start HEAD ref on a real transition (soft — must not
        # block the phase advance). Mirrors the fast path so every real
        # transition records the entry-point HEAD, not just contract-less ones;
        # skipped on a same-phase no-op so the entry ref is not pushed forward.
        if is_real_transition:
            _stamp_phase_start_ref(store, project_root)
        _spawn_background(_trigger_compose_in_process(store, contract_id, request))
    except HTTPException:
        raise
    except Exception:
        # Transaction rolled back by context manager
        raise HTTPException(
            status_code=500,
            detail="Phase advance failed — transaction rolled back",
        ) from None

    return PhaseAdvanceResponse(
        kind=result.kind,
        value=phase_value,  # the bare name, not the stored blob
        owner=result.owner,
        lease_expires_at=result.lease_expires_at,
        contract_id=contract_id,
        end_session_instruction=end_session_instruction(phase_value),
        gate_verdict=None,
    )


# ---------------------------------------------------------------------------
# POST /state/cursor
# ---------------------------------------------------------------------------


@router.post(
    "/cursor",
    response_model=StateWriteResponse | StateConflictInfo,
    responses={
        409: {"model": StateConflictInfo, "description": "Lease conflict"},
    },
    summary="Set the work-item cursor",
)
async def write_cursor(
    req: StateWriteRequest,
    store: DuckDBStateStore = Depends(get_repo_store),
) -> StateWriteResponse | StateConflictInfo:
    result = await asyncio.to_thread(store.write, "cursor", req.value, owner=req.owner)
    http_status, response = _write_result_to_response(result)
    if http_status != 200:
        raise HTTPException(
            status_code=409,
            detail=response.model_dump(mode="json"),  # type: ignore[union-attr]
        )
    return response  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Scoped cursor endpoints — per-session cursors (task / phase / proxy)
# ---------------------------------------------------------------------------


@router.post(
    "/cursors/{session_key}",
    response_model=StateWriteResponse | StateConflictInfo,
    responses={
        409: {"model": StateConflictInfo, "description": "Lease conflict"},
    },
    summary="Set a per-session cursor",
)
async def write_scoped_cursor(
    session_key: str,
    req: StateWriteRequest,
    store: DuckDBStateStore = Depends(get_repo_store),
) -> StateWriteResponse | StateConflictInfo:
    result = await asyncio.to_thread(store.set_scoped_cursor, session_key, req.value)
    http_status, response = _write_result_to_response(result)
    if http_status != 200:
        raise HTTPException(
            status_code=409,
            detail=response.model_dump(mode="json"),  # type: ignore[union-attr]
        )
    return response  # type: ignore[return-value]


@router.get(
    "/cursors/{session_key}",
    response_model=StateReadResponse,
    summary="Get a per-session cursor",
)
async def read_scoped_cursor(
    session_key: str,
    store: DuckDBStateStore = Depends(get_repo_store),
) -> StateReadResponse:
    value = await asyncio.to_thread(store.get_scoped_cursor, session_key)
    return StateReadResponse(kind="cursor", value=value)


@router.delete(
    "/cursors/{session_key}",
    response_model=StateReadResponse,
    summary="Delete a per-session cursor",
)
async def delete_scoped_cursor(
    session_key: str,
    store: DuckDBStateStore = Depends(get_repo_store),
) -> StateReadResponse:
    await asyncio.to_thread(store._delete_scoped_cursor, session_key)
    return StateReadResponse(kind="cursor", value=None)


# ---------------------------------------------------------------------------
# POST /state/approve
# ---------------------------------------------------------------------------


@router.post(
    "/approve",
    response_model=StateWriteResponse | StateConflictInfo,
    responses={
        409: {"model": StateConflictInfo, "description": "Lease conflict"},
    },
    summary="Record an approval for a phase",
)
async def write_approve(
    req: StateWriteRequest,
    store: DuckDBStateStore = Depends(get_repo_store),
) -> StateWriteResponse | StateConflictInfo:
    result = await asyncio.to_thread(
        store.write,
        "approved",
        req.value,
        session_key=req.session_key,
        owner=req.owner,
    )
    http_status, response = _write_result_to_response(result)
    if http_status != 200:
        raise HTTPException(
            status_code=409,
            detail=response.model_dump(mode="json"),  # type: ignore[union-attr]
        )
    return response  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# POST /state/import-files
# ---------------------------------------------------------------------------


@router.post(
    "/import-files",
    summary="One-shot migration of a repo's .agentalloy file mirror into the store",
)
async def import_files(
    root: Path = Depends(resolve_repo_root),
    store: DuckDBStateStore = Depends(get_repo_store),
) -> dict[str, dict[str, str]]:
    """Migrate legacy file-based state (``.agentalloy/phase``, etc.) into the store.

    One-shot migration: carries the file mirror into DuckDB and deletes the
    phase file once its content has been stored. Idempotent — a repo with no
    file mirror, or one already migrated, returns an empty map. Deliberately
    *not* subject to the phase-advance gate: this carries a phase that already
    exists across a storage change, it does not advance one.

    After migration the store is the sole source of truth. Sidecar harnesses
    that still watch ``.agentalloy/phase`` will see the file deleted; they must
    be re-wired to use the proxy or the in-process store hook.
    """
    try:
        imported = await asyncio.to_thread(store.import_from_files, root / ".agentalloy")
    except StateStoreError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"imported": imported}


# ---------------------------------------------------------------------------
# POST /contracts — create
# ---------------------------------------------------------------------------


@contract_router.post(
    "",
    response_model=ContractResponse,
    responses={
        409: {"description": "Contract ID already exists"},
    },
    summary="Create a new contract",
)
async def create_contract(
    req: ContractCreateRequest,
    request: Request,
    store: DuckDBStateStore = Depends(get_repo_store),
) -> ContractResponse:
    cid = await asyncio.to_thread(
        store.put_contract,
        req.contract_id,
        phase=req.phase,
        slug=req.slug,
        work_item=req.work_item,
        route=req.route,
        domain_tags=req.domain_tags,
        scope_touches=req.scope_touches,
        scope_avoids=req.scope_avoids,
        success_criteria=req.success_criteria,
        body=req.body,
    )

    # Trigger compose in-process — the write is the trigger
    _spawn_background(_trigger_compose_in_process(store, cid, request))

    row = store.get_contract(cid)
    if row is None:
        raise HTTPException(status_code=500, detail="Contract disappeared after write")
    return _contract_row_to_response(row)


# ---------------------------------------------------------------------------
# GET /contracts — list with filters
# ---------------------------------------------------------------------------


@contract_router.get(
    "",
    response_model=ContractListResponse,
    summary="List contracts with optional filters",
)
async def list_contracts(
    phase: str | None = Query(default=None, description="Filter by phase"),
    slug: str | None = Query(default=None, description="Filter by slug"),
    work_item: str | None = Query(default=None, description="Filter by work-item slug"),
    status: str | None = Query(default=None, description="Filter by status"),
    store: DuckDBStateStore = Depends(get_repo_store),
) -> ContractListResponse:
    rows = await asyncio.to_thread(
        store.list_contracts,
        phase=phase,
        slug=slug,
        work_item=work_item,
        status=status,
    )
    return ContractListResponse(contracts=[_contract_row_to_response(row) for row in rows])


# ---------------------------------------------------------------------------
# GET /contracts/{id} — read single
# ---------------------------------------------------------------------------


@contract_router.get(
    "/{contract_id:path}",
    response_model=ContractResponse,
    responses={
        404: {"description": "Contract not found"},
    },
    summary="Get a contract by ID",
)
async def get_contract(
    contract_id: str,
    store: DuckDBStateStore = Depends(get_repo_store),
) -> ContractResponse:
    row = await asyncio.to_thread(store.get_contract, contract_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Contract {contract_id!r} not found")
    return _contract_row_to_response(row)


# ---------------------------------------------------------------------------
# PATCH /contracts/{id} — in-place correction
# ---------------------------------------------------------------------------


@contract_router.patch(
    "/{contract_id:path}",
    response_model=ContractResponse,
    responses={
        404: {"description": "Contract not found"},
    },
    summary="In-place correction of a contract",
)
async def patch_contract(
    contract_id: str,
    req: ContractPatchRequest,
    store: DuckDBStateStore = Depends(get_repo_store),
) -> ContractResponse:
    updated = await asyncio.to_thread(
        store.update_contract,
        contract_id,
        body=req.body,
        domain_tags=req.domain_tags,
        scope_touches=req.scope_touches,
        scope_avoids=req.scope_avoids,
        success_criteria=req.success_criteria,
    )
    if not updated:
        raise HTTPException(status_code=404, detail=f"Contract {contract_id!r} not found")
    row = store.get_contract(contract_id)
    if row is None:
        raise HTTPException(status_code=500, detail="Contract disappeared after update")
    return _contract_row_to_response(row)


# ---------------------------------------------------------------------------
# POST /contracts/{id}/archive
# ---------------------------------------------------------------------------


@contract_router.post(
    "/{contract_id:path}/archive",
    response_model=ContractResponse,
    responses={
        404: {"description": "Contract not found or already archived"},
    },
    summary="Archive a contract",
)
async def archive_contract(
    contract_id: str,
    store: DuckDBStateStore = Depends(get_repo_store),
) -> ContractResponse:
    archived = await asyncio.to_thread(store.archive_contract, contract_id)
    if not archived:
        raise HTTPException(
            status_code=404,
            detail=f"Contract {contract_id!r} not found or already archived",
        )
    row = store.get_contract(contract_id)
    if row is None:
        raise HTTPException(status_code=500, detail="Contract disappeared after archive")
    return _contract_row_to_response(row)


# ---------------------------------------------------------------------------
# POST /contracts/{id}/supersede
# ---------------------------------------------------------------------------


@contract_router.post(
    "/{contract_id:path}/supersede",
    response_model=ContractResponse,
    responses={
        404: {"description": "Contract not found"},
        400: {"description": "Cannot supersede contract in current status"},
    },
    summary="Supersede a contract with a new revision",
)
async def supersede_contract(
    contract_id: str,
    req: ContractSupersedeRequest,
    request: Request,
    store: DuckDBStateStore = Depends(get_repo_store),
) -> ContractResponse:
    new_id = await asyncio.to_thread(
        store.supersede_contract,
        contract_id,
        new_contract_id=req.new_contract_id,
        phase=req.phase,
        slug=req.slug,
        work_item=req.work_item,
        route=req.route,
        domain_tags=req.domain_tags,
        scope_touches=req.scope_touches,
        scope_avoids=req.scope_avoids,
        success_criteria=req.success_criteria,
        body=req.body,
    )

    # Trigger compose in-process for the new contract
    _spawn_background(_trigger_compose_in_process(store, new_id, request))

    row = store.get_contract(new_id)
    if row is None:
        raise HTTPException(status_code=500, detail="New contract disappeared after write")
    return _contract_row_to_response(row)


# ---------------------------------------------------------------------------
# PUT/GET /state/artifact — deliverable artifact bodies (docs/spec/<slug>.md,
# docs/design/<slug>/{approach,tasks,test-plan}.md), store-backed replacement
# for the on-disk scaffolding these used to require.
# ---------------------------------------------------------------------------


@router.put(
    "/artifact",
    response_model=ArtifactResponse,
    summary="Upsert a deliverable artifact body",
)
async def set_artifact(
    req: ArtifactSetRequest,
    store: DuckDBStateStore = Depends(get_repo_store),
) -> ArtifactResponse:
    row = await asyncio.to_thread(store.set_artifact, req.phase, req.slug, req.name, req.content)

    # When phase=spec, parse AC headings from the artifact body and update
    # the contract's success_criteria if any AC-N headings are found.
    if req.phase == "spec":
        from agentalloy.contracts import parse_ac_headings

        ac_headings = parse_ac_headings(req.content)
        if ac_headings:
            # Find the existing contract for this slug in spec phase. Contract
            # IDs are not a fixed f"{phase}/{slug}" (callers store the bare slug
            # or a filename stem), so resolve by the (phase, slug) pair instead
            # of constructing an ID that get_contract would never find.
            existing_rows = await asyncio.to_thread(
                store.list_contracts,
                phase=req.phase,
                slug=req.slug,
                status="active",
            )
            if existing_rows:
                existing = existing_rows[0]
                contract_id = existing["contract_id"]
                # Merge: new ACs from artifact, preserve any existing ones not overwritten
                existing_criteria = existing.get("success_criteria") or []
                ac_ids = {h["id"] for h in ac_headings}
                merged = [
                    c
                    for c in existing_criteria
                    if not (isinstance(c, dict) and c.get("id") in ac_ids)
                ]
                merged.extend(ac_headings)
                await asyncio.to_thread(store.update_contract, contract_id, success_criteria=merged)

    return ArtifactResponse(**row)


# ---------------------------------------------------------------------------
# GET /phases/{phase}/contracts — list contracts for a phase (ordered by
# contract_id, which preserves filename/numeric ordering)
# ---------------------------------------------------------------------------


@contract_router.get(
    "/phases/{phase}/contracts",
    response_model=ContractListResponse,
    summary="List active contracts for a phase",
)
async def list_contracts_for_phase(
    phase: str,
    store: DuckDBStateStore = Depends(get_repo_store),
) -> ContractListResponse:
    rows = await asyncio.to_thread(store.list_contracts_for_phase, phase)
    return ContractListResponse(
        contracts=[_contract_row_to_response(row) for row in rows],
    )


# ---------------------------------------------------------------------------
# POST /state/migrate-disk-contracts — one-shot migration of legacy .md
# contract files into the store (upgrade hook)
# ---------------------------------------------------------------------------


@router.post(
    "/migrate-disk-contracts",
    summary="Migrate disk-based contract files into the store",
)
async def migrate_disk_contracts(
    root: Path = Depends(resolve_repo_root),
    store: DuckDBStateStore = Depends(get_state_store),
) -> dict[str, Any]:
    """Migrate legacy ``.agentalloy/contracts/`` files into the store.

    Returns ``{"migrated": int, "errors": int, "details": [str]}``.
    """
    result = await asyncio.to_thread(
        store.migrate_disk_contracts,
        [str(root)],
    )
    return result
