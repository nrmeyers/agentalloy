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
from agentalloy.providers.base import end_session_instruction
from agentalloy.storage.state_store import DuckDBStateStore, StateStoreError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/state", tags=["state"])
contract_router = APIRouter(prefix="/contracts", tags=["contracts"])


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
    """Slug a repo root, memoised — ``repo_slug`` shells out to git.

    Imported from ``code_index.slug`` deliberately: that module is the canonical
    worktree-aware implementation and is pure stdlib, so importing it does not
    drag in the ``[code-index]`` extra.
    """
    from agentalloy.code_index.slug import repo_slug

    try:
        return repo_slug(Path(root))
    except Exception:
        logger.debug("repo_slug failed for %s — falling back to basename", root, exc_info=True)
        return Path(root).name


def get_repo_store(
    root: Path = Depends(resolve_repo_root),
    store: DuckDBStateStore = Depends(get_state_store),
) -> DuckDBStateStore:
    """The lifespan store, scoped to the repo this request is about.

    One service serves every repo from one ``state.duck``; without this scoping
    they all share a single bucket and a phase set in one repo is read by the
    next.  Repo-scoped stores isolate each repo's phase row behind a key namespace.
    """
    return store.for_repo(_repo_key_for(str(root)))


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


def _rewrite_posture(root: Path, phase: str) -> list[str]:
    """Rewrite enforcement posture files for wired Tier A harnesses.

    Thin wrapper around ``rewrite_enforcement_posture`` so the router can
    call it from ``asyncio.to_thread``.  Soft: any failure is logged and
    swallowed — a posture rewrite failure must not block the phase advance.
    """
    try:
        from agentalloy.install.subcommands.wire_harness import (
            rewrite_enforcement_posture as _rewrite,
        )

        return _rewrite(root, phase)
    except Exception:
        logger.debug("posture rewrite failed for %s phase=%s", root, phase, exc_info=True)
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

    task = asyncio.create_task(_run())
    # Prevent GC: store the task on app.state keyed by contract_id.
    # The task self-removes on completion via the done callback.
    state = request.app.state
    tasks = getattr(state, "compose_tasks", None)
    if tasks is None:
        tasks = {}
        state.compose_tasks = tasks
    task.add_done_callback(lambda t: tasks.pop(contract_id, None))
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
    without them, had no way to reach ``mode``/``free_since``/``transitioned_by``
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
        free_since=phase.free_since,
        transitioned_by=phase.transitioned_by,
        started_at=phase.started_at,
        last_updated=phase.last_updated,
        workflow=phase.workflow or None,
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
        default=None, description="Session-scoped kinds (and phase-scoped 'approved') use this."
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


def _evaluate_phase_gate(
    store: DuckDBStateStore,
    current_phase: str | None,
    target_phase: str,
    project_root: Path | None,
    override: bool,
) -> dict[str, Any] | None:
    """Evaluate the exit gate for a phase transition.

    Delegates to :func:`agentalloy.signals.gates.evaluate_phase_gate` so the
    route and the CLI share a single evaluation point.
    """
    from agentalloy.signals.gates import evaluate_phase_gate as _eval  # noqa: PLC0415

    return _eval(current_phase, target_phase, project_root, override, store)


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
    so silently discards ``mode``, ``free_since``, ``started_at`` and
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
    verdict = _evaluate_phase_gate(store, current_phase, phase_value, project_root, req.override)
    if verdict is not None:
        # Gate blocks the transition — return verdict without writing.
        return PhaseAdvanceResponse(
            kind="phase",
            value=phase_value,
            gate_verdict=verdict,
        )

    # Fast path: no contract — use the existing non-transactional path
    if req.contract is None:
        result = await asyncio.to_thread(
            store.write_phase,
            phase_value,
            actor=actor,
            owner=req.owner,
            mode=req.mode,
            free_since=req.free_since,
        )
        http_status, response = _write_result_to_response(result)
        if http_status != 200:
            raise HTTPException(
                status_code=409,
                detail=response.model_dump(mode="json"),  # type: ignore[union-attr]
            )
        # Posture rewrite after successful phase advance
        if repo_root is not None:
            await asyncio.to_thread(_rewrite_posture, Path(repo_root), phase_value)
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
    try:
        with store.transaction() as tx:
            # Write the phase — blob semantics, reusing this transaction.
            result = tx.write_phase(
                phase_value,
                actor=actor,
                owner=req.owner,
                mode=req.mode,
                free_since=req.free_since,
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
        # Transaction committed — posture rewrite + in-process compose
        if repo_root is not None:
            await asyncio.to_thread(_rewrite_posture, Path(repo_root), phase_value)
        asyncio.create_task(_trigger_compose_in_process(store, contract_id, request))
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
        store.write, "approved", req.value, session_key=req.session_key, owner=req.owner
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
    asyncio.create_task(_trigger_compose_in_process(store, cid, request))

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
    status: str | None = Query(default=None, description="Filter by status"),
    store: DuckDBStateStore = Depends(get_repo_store),
) -> ContractListResponse:
    rows = await asyncio.to_thread(store.list_contracts, phase=phase, slug=slug, status=status)
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
    asyncio.create_task(_trigger_compose_in_process(store, new_id, request))

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
    return ArtifactResponse(**row)


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
    rows = await asyncio.to_thread(store.list_artifacts, phase, slug=slug, name_glob=name_glob)
    return ArtifactListResponse(artifacts=[ArtifactResponse(**row) for row in rows])
