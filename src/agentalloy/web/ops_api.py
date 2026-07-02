"""Web UI operations endpoints — repos dashboard, approval queue, doctor,
packs, reembed status, profiles.

Read paths reuse the CLI's own helpers (install state, phase/gate evaluation,
doctor checks, pack discovery, profile resolution) so the dashboard can never
drift from what the CLI reports. The two mutations here — approve and reembed —
wrap ``run_approve`` and ``run_bulk_reembed`` and require the
``X-AgentAlloy-CSRF: 1`` header.

Doctor repair is deliberately NOT exposed: its repair sequence (migrate →
install-packs → reembed) wants the service's read-only store handle closed,
which the process serving this endpoint holds. The UI renders the CLI command
instead.
"""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel

from agentalloy.web.config_api import _require_csrf

router = APIRouter()


# ---------------------------------------------------------------------------
# repos
# ---------------------------------------------------------------------------


class RepoInfo(BaseModel):
    repo_root: str
    harnesses: list[str]
    exists: bool
    phase: str | None
    lifecycle_mode: str | None
    profile: str | None
    upstream_url: str | None
    upstream_model: str | None
    cursor: str | None
    contracts_by_phase: dict[str, int]
    approval_required: bool
    approval_pending: bool


class ReposResponse(BaseModel):
    total: int
    repos: list[RepoInfo]


def _wired_repos() -> dict[str, list[str]]:
    """repo_root → harness names, from install state's wiring records."""
    from agentalloy.install import state as install_state

    st = install_state.load_state()
    grouped: dict[str, list[str]] = {}
    for entry in st.get("harness_files_written") or []:
        root = str(entry.get("repo_root") or "")
        harness = str(entry.get("harness") or "")
        if not root:
            continue
        grouped.setdefault(root, [])
        if harness and harness not in grouped[root]:
            grouped[root].append(harness)
    return grouped


def _approval_state(root: Path, phase: str | None) -> tuple[bool, bool]:
    """(required, pending) — pending means required and not satisfied/stale."""
    from agentalloy.install.subcommands.phase import (
        _APPROVAL_SINCE,  # pyright: ignore[reportPrivateUsage]
    )
    from agentalloy.signals.predicates import approval_marker_path, approval_required

    if phase is None or not approval_required(phase):
        return (False, False)
    marker = approval_marker_path(root, phase)
    if not marker.is_file():
        return (True, True)
    since = _APPROVAL_SINCE.get(phase)
    if since:
        newest = _newest_mtime(root, since)
        if newest is not None and newest > marker.stat().st_mtime:
            return (True, True)  # stale marker re-blocks
    return (True, False)


def _newest_mtime(root: Path, glob: str) -> float | None:
    try:
        mtimes = [p.stat().st_mtime for p in root.glob(glob) if p.is_file()]
    except OSError:
        return None
    return max(mtimes) if mtimes else None


def _repo_info(root_str: str, harnesses: list[str]) -> RepoInfo:
    from agentalloy.api.proxy_context import read_upstream
    from agentalloy.install.subcommands.status import (
        _repo_phase,  # pyright: ignore[reportPrivateUsage]
    )
    from agentalloy.profiles import detect_profile
    from agentalloy.signals.skill_loader import (
        _read_cursor,  # pyright: ignore[reportPrivateUsage]
        _read_lifecycle_mode,  # pyright: ignore[reportPrivateUsage]
    )

    root = Path(root_str)
    if not root.is_dir():
        return RepoInfo(
            repo_root=root_str,
            harnesses=harnesses,
            exists=False,
            phase=None,
            lifecycle_mode=None,
            profile=None,
            upstream_url=None,
            upstream_model=None,
            cursor=None,
            contracts_by_phase={},
            approval_required=False,
            approval_pending=False,
        )
    phase = _repo_phase(root_str)
    upstream = read_upstream(root)
    contracts: dict[str, int] = {}
    contracts_dir = root / ".agentalloy" / "contracts"
    if contracts_dir.is_dir():
        for phase_dir in sorted(contracts_dir.iterdir()):
            if phase_dir.is_dir():
                n = len(list(phase_dir.glob("*.md")))
                if n:
                    contracts[phase_dir.name] = n
    try:
        profile = detect_profile(root).name
    except Exception:  # noqa: BLE001 — profile detection is decoration here
        profile = None
    required, pending = _approval_state(root, phase)
    return RepoInfo(
        repo_root=root_str,
        harnesses=harnesses,
        exists=True,
        phase=phase,
        lifecycle_mode=_read_lifecycle_mode(root),
        profile=profile,
        upstream_url=upstream.url if upstream else None,
        upstream_model=upstream.model if upstream else None,
        cursor=_read_cursor(root),
        contracts_by_phase=contracts,
        approval_required=required,
        approval_pending=pending,
    )


@router.get("/api/repos", response_model=ReposResponse, summary="Wired repos with live state")
async def list_repos() -> ReposResponse:
    def _build() -> ReposResponse:
        repos = [_repo_info(root, harnesses) for root, harnesses in sorted(_wired_repos().items())]
        return ReposResponse(total=len(repos), repos=repos)

    return await asyncio.to_thread(_build)


class GateStatus(BaseModel):
    repo: str
    phase: str | None
    next_phase: str | None
    blocked: bool
    advisories: list[str]
    approval_required: bool
    approval_pending: bool
    approver: str | None
    approved_at: str | None


@router.get(
    "/api/repos/gates",
    response_model=GateStatus,
    summary="Deterministic exit-gate status for a repo's current phase",
)
async def repo_gates(repo: str = Query(...)) -> GateStatus:
    root = Path(repo)
    if not root.is_dir():
        raise HTTPException(status_code=400, detail=f"repo is not a directory: {repo}")

    def _build() -> GateStatus:
        from agentalloy.install.subcommands.phase import (
            _forward_gate_blocks,  # pyright: ignore[reportPrivateUsage]
        )
        from agentalloy.install.subcommands.status import (
            _repo_phase,  # pyright: ignore[reportPrivateUsage]
        )
        from agentalloy.signals.gates import _PHASE_GRAPH  # pyright: ignore[reportPrivateUsage]
        from agentalloy.signals.predicates import approval_marker_path

        phase = _repo_phase(repo)
        nxt = _PHASE_GRAPH.get(phase) if phase else None
        blocked, advisories = (False, [])
        if phase and nxt and nxt != phase:
            blocked, advisories = _forward_gate_blocks(phase, nxt, root)
        required, pending = _approval_state(root, phase)
        approver = approved_at = None
        if phase and required and not pending:
            marker = approval_marker_path(root, phase)
            for line in marker.read_text().splitlines():
                if line.startswith("approver:"):
                    approver = line.partition(":")[2].strip()
                elif line.startswith("approved_at:"):
                    approved_at = line.partition(":")[2].strip().strip('"')
        return GateStatus(
            repo=repo,
            phase=phase,
            next_phase=nxt,
            blocked=blocked,
            advisories=advisories,
            approval_required=required,
            approval_pending=pending,
            approver=approver,
            approved_at=approved_at,
        )

    return await asyncio.to_thread(_build)


# ---------------------------------------------------------------------------
# approvals
# ---------------------------------------------------------------------------


class PendingApproval(BaseModel):
    repo: str
    phase: str
    next_phase: str | None
    stale: bool  # marker exists but the artifact changed after sign-off
    artifacts: list[str]


class ApprovalsResponse(BaseModel):
    total: int
    pending: list[PendingApproval]


@router.get(
    "/api/approvals",
    response_model=ApprovalsResponse,
    summary="Pending approval gates across all wired repos",
)
async def list_approvals() -> ApprovalsResponse:
    def _build() -> ApprovalsResponse:
        from agentalloy.install.subcommands.phase import (
            _APPROVAL_SINCE,  # pyright: ignore[reportPrivateUsage]
        )
        from agentalloy.install.subcommands.status import (
            _repo_phase,  # pyright: ignore[reportPrivateUsage]
        )
        from agentalloy.signals.gates import _PHASE_GRAPH  # pyright: ignore[reportPrivateUsage]
        from agentalloy.signals.predicates import approval_marker_path

        pending: list[PendingApproval] = []
        for root_str in sorted(_wired_repos()):
            root = Path(root_str)
            if not root.is_dir():
                continue
            phase = _repo_phase(root_str)
            required, is_pending = _approval_state(root, phase)
            if not (phase and required and is_pending):
                continue
            glob = _APPROVAL_SINCE.get(phase)
            artifacts = (
                sorted(str(p.relative_to(root)) for p in root.glob(glob) if p.is_file())
                if glob
                else []
            )
            if glob and not artifacts:
                # Nothing to approve yet — `approve` would refuse ("no exit
                # artifact"). The gate is still a blocker (see /api/repos/gates),
                # but the queue lists only actionable sign-offs.
                continue
            pending.append(
                PendingApproval(
                    repo=root_str,
                    phase=phase,
                    next_phase=_PHASE_GRAPH.get(phase),
                    stale=approval_marker_path(root, phase).is_file(),
                    artifacts=artifacts,
                )
            )
        return ApprovalsResponse(total=len(pending), pending=pending)

    return await asyncio.to_thread(_build)


class ApproveRequest(BaseModel):
    repo: str
    phase: str
    approver: str | None = None


@router.post(
    "/api/repos/approve",
    summary="Record a human approval marker (auto-advances the phase)",
)
async def approve(
    body: ApproveRequest,
    x_agentalloy_csrf: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _require_csrf(x_agentalloy_csrf)
    root = Path(body.repo)
    if not root.is_dir():
        raise HTTPException(status_code=400, detail=f"repo is not a directory: {body.repo}")
    from agentalloy.install.subcommands.approve import run_approve

    result = await asyncio.to_thread(run_approve, body.phase, root, body.approver)
    if not result.get("ok"):
        raise HTTPException(
            status_code=409, detail={"error": "approve_refused", "detail": result.get("error")}
        )
    return result


# ---------------------------------------------------------------------------
# ops: doctor / packs / reembed / profiles
# ---------------------------------------------------------------------------


@router.get("/api/doctor", summary="Run doctor checks (read-only)")
async def doctor() -> dict[str, Any]:
    from agentalloy.install.subcommands.doctor import run_doctor

    return await asyncio.to_thread(run_doctor)


class PackInfo(BaseModel):
    name: str
    version: str | None
    tier: str | None
    description: str | None
    skill_count: int
    installed_count: int


class PacksResponse(BaseModel):
    total: int
    packs: list[PackInfo]


@router.get("/api/packs", response_model=PacksResponse, summary="Bundled packs + install state")
async def list_packs(request: Request) -> PacksResponse:
    def _build() -> PacksResponse:
        import agentalloy
        from agentalloy.install.subcommands.install_packs import (
            _discover_packs,  # pyright: ignore[reportPrivateUsage]
        )

        packs_root = Path(agentalloy.__file__).resolve().parent / "_packs"
        manifests = _discover_packs(packs_root)
        runtime = getattr(request.app.state, "runtime", None)
        corpus_ids: set[str] = set()
        if runtime is not None:
            corpus_ids = {s.skill_id for s in runtime.get_active_skills()}
        packs: list[PackInfo] = []
        for name, manifest in sorted(manifests.items()):
            skill_ids = [
                str(e.get("skill_id")) for e in manifest.get("skills") or [] if e.get("skill_id")
            ]
            packs.append(
                PackInfo(
                    name=name,
                    version=manifest.get("version"),
                    tier=manifest.get("tier"),
                    description=(manifest.get("description") or "").strip() or None,
                    skill_count=len(skill_ids),
                    installed_count=len([s for s in skill_ids if s in corpus_ids]),
                )
            )
        return PacksResponse(total=len(packs), packs=packs)

    return await asyncio.to_thread(_build)


class ReembedStatus(BaseModel):
    embedded_total: int
    unembedded: int


@router.get(
    "/api/reembed/status",
    response_model=ReembedStatus,
    summary="Embedded vs pending fragment counts",
)
async def reembed_status(request: Request) -> ReembedStatus:
    store = getattr(request.app.state, "store", None)
    vector_store = getattr(request.app.state, "vector_store", None)
    if store is None or vector_store is None:
        raise HTTPException(status_code=503, detail="stores unavailable")

    def _build() -> ReembedStatus:
        from agentalloy.reembed.cli import discover_unembedded_fragments

        pending = discover_unembedded_fragments(store, vector_store)
        return ReembedStatus(
            embedded_total=int(vector_store.count_embeddings()),
            unembedded=len(pending),
        )

    return await asyncio.to_thread(_build)


class ReembedRequest(BaseModel):
    dry_run: bool = True


@router.post("/api/reembed", summary="Run a bulk reembed pass (or dry-run count)")
async def reembed(
    request: Request,
    body: ReembedRequest,
    x_agentalloy_csrf: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _require_csrf(x_agentalloy_csrf)
    if body.dry_run:
        status = await reembed_status(request)
        return {"dry_run": True, "would_embed": status.unembedded}

    def _run() -> dict[str, Any]:
        from agentalloy.reembed.cli import run_bulk_reembed
        from agentalloy.web.runtime_refresh import refresh_runtime_cache

        sink: dict[str, Any] = {}
        # This process holds the skill store read-only for its lifetime, and
        # DuckDB grants the reembed writer only while nothing else has the
        # file open — release the handle for the duration, reconnect after,
        # then reload the cache so the new corpus serves without a restart.
        store = getattr(request.app.state, "store", None)
        release = store.released() if store is not None else nullcontext()
        with release:
            rc = run_bulk_reembed(no_restart=True, result_sink=sink)
        refreshed = refresh_runtime_cache(request.app)
        return {"dry_run": False, "exit_code": rc, "cache_refreshed": refreshed, **sink}

    return await asyncio.to_thread(_run)


class ProfileEntry(BaseModel):
    name: str
    is_default: bool
    active_for_cwd: bool
    match_remote: list[str]
    match_path: list[str]
    has_overrides: bool


class ProfilesResponse(BaseModel):
    total: int
    profiles: list[ProfileEntry]


@router.get("/api/profiles", response_model=ProfilesResponse, summary="Configured profiles")
async def profiles() -> ProfilesResponse:
    def _build() -> ProfilesResponse:
        from agentalloy.profiles import list_profiles

        entries = [
            ProfileEntry(
                name=str(p.get("name")),
                is_default=bool(p.get("is_default")),
                active_for_cwd=bool(p.get("active_for_cwd")),
                match_remote=list(p.get("match_remote") or []),
                match_path=list(p.get("match_path") or []),
                has_overrides=bool(p.get("has_overrides")),
            )
            for p in list_profiles()
        ]
        return ProfilesResponse(total=len(entries), profiles=entries)

    return await asyncio.to_thread(_build)


class ProfileResolveRequest(BaseModel):
    repo: str


@router.post("/api/profiles/resolve", summary="Which profile does a repo resolve to?")
async def resolve_profile(body: ProfileResolveRequest) -> dict[str, Any]:
    root = Path(body.repo)
    if not root.is_dir():
        raise HTTPException(status_code=400, detail=f"repo is not a directory: {body.repo}")

    def _build() -> dict[str, Any]:
        from agentalloy.profiles import detect_profile

        p = detect_profile(root)
        return {"repo": body.repo, "profile": p.name, "is_default": p.is_default}

    return await asyncio.to_thread(_build)
