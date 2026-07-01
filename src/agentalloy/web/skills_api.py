"""Web UI skill browser/editor endpoints + signal simulator.

- ``GET /api/skills`` — filterable corpus listing with provenance (which
  shipped pack a skill came from, whether an override layer is active).
- ``GET /api/skills/{skill_id}/versions`` — full version history (the
  ``skill_versions`` table survived the v5 rebuild; only the runtime reads
  were active-version-only).
- ``GET/PUT/DELETE /api/skills/{skill_id}/override`` — the web twin of
  ``agentalloy customize``: one atomic validate+save instead of the CLI's
  edit → validate → update loop. Only ``raw_prose`` and ``domain_tags`` are
  writable; workflow structural fields are product-owned and rejected by the
  shared validator, which also enforces ``prose_invariants``.
- ``POST /api/signal/evaluate`` — read-only signal simulator ("why did/didn't
  composition fire?") via ``evaluate_signal(mutate=False)``.

Mutations require the ``X-AgentAlloy-CSRF: 1`` header (see config_api).
"""

from __future__ import annotations

import asyncio
import contextlib
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

import yaml
from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel

from agentalloy.web.config_api import _require_csrf

router = APIRouter()

_OVERRIDABLE_CLASSES = ("system", "workflow")


# ---------------------------------------------------------------------------
# provenance
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _pack_by_skill_id() -> dict[str, str]:
    """skill_id → shipped pack name, from the wheel-bundled pack manifests."""
    import agentalloy

    packs_dir = Path(agentalloy.__file__).resolve().parent / "_packs"
    mapping: dict[str, str] = {}
    for manifest in sorted(packs_dir.glob("*/pack.yaml")):
        try:
            data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001 — a broken manifest shouldn't kill the listing
            continue
        pack_name = str(data.get("name") or manifest.parent.name)
        for entry in data.get("skills") or []:
            sid = entry.get("skill_id")
            if sid:
                mapping[str(sid)] = pack_name
    return mapping


def _override_layer(skill_id: str) -> str | None:
    """Active override layer for a system/workflow skill, or None. Best-effort."""
    from agentalloy.install.subcommands import customize

    try:
        layers = customize._resolve_skill_layers(skill_id, None)  # pyright: ignore[reportPrivateUsage]
        layer, path = customize._active_layer(layers)  # pyright: ignore[reportPrivateUsage]
    except Exception:  # noqa: BLE001 — provenance is decoration, never a 500
        return None
    return layer if layer != "default" and path is not None else None


# ---------------------------------------------------------------------------
# listing + versions
# ---------------------------------------------------------------------------


class SkillSummary(BaseModel):
    skill_id: str
    canonical_name: str
    category: str
    skill_class: str
    domain_tags: list[str]
    phase_scope: list[str] | None
    tier: str | None
    description: str | None
    always_apply: bool
    pack: str | None
    override_layer: str | None


class SkillListResponse(BaseModel):
    total: int
    skills: list[SkillSummary]


class SkillVersion(BaseModel):
    version_id: str
    version_number: int
    authored_at: str | None
    author: str
    change_summary: str
    status: str | None
    raw_prose: str
    is_active: bool


class SkillVersionsResponse(BaseModel):
    skill_id: str
    versions: list[SkillVersion]


def _active_skills(request: Request) -> list[Any]:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is not None:
        return list(runtime.get_active_skills())
    store = getattr(request.app.state, "store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="skill store unavailable")
    from agentalloy.reads.active import get_active_skills

    return list(get_active_skills(store))


@router.get("/api/skills", response_model=SkillListResponse, summary="List/filter the corpus")
async def list_skills(
    request: Request,
    skill_class: str | None = Query(default=None, alias="class"),
    category: str | None = Query(default=None),
    phase: str | None = Query(default=None),
    q: str | None = Query(default=None, description="substring over id/name/description/tags"),
) -> SkillListResponse:
    def _build() -> SkillListResponse:
        pack_map = _pack_by_skill_id()
        out: list[SkillSummary] = []
        for s in _active_skills(request):
            if skill_class and s.skill_class != skill_class:
                continue
            if category and s.category != category:
                continue
            if phase and s.phase_scope and phase not in s.phase_scope:
                continue
            if q:
                hay = " ".join(
                    [s.skill_id, s.canonical_name, s.description or "", *s.domain_tags]
                ).lower()
                if q.lower() not in hay:
                    continue
            out.append(
                SkillSummary(
                    skill_id=s.skill_id,
                    canonical_name=s.canonical_name,
                    category=s.category,
                    skill_class=s.skill_class,
                    domain_tags=list(s.domain_tags),
                    phase_scope=list(s.phase_scope) if s.phase_scope else None,
                    tier=s.tier,
                    description=s.description,
                    always_apply=s.always_apply,
                    pack=pack_map.get(s.skill_id),
                    override_layer=(
                        _override_layer(s.skill_id)
                        if s.skill_class in _OVERRIDABLE_CLASSES
                        else None
                    ),
                )
            )
        out.sort(key=lambda x: (x.skill_class, x.category, x.skill_id))
        return SkillListResponse(total=len(out), skills=out)

    return await asyncio.to_thread(_build)


@router.get(
    "/api/skills/{skill_id}/versions",
    response_model=SkillVersionsResponse,
    summary="Full version history for a skill",
)
async def skill_versions(request: Request, skill_id: str) -> SkillVersionsResponse:
    store = getattr(request.app.state, "store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="skill store unavailable")

    def _query() -> SkillVersionsResponse:
        active_row = store.execute(
            "SELECT current_version_id FROM skills WHERE skill_id = $sid", {"sid": skill_id}
        )
        active_rows = list(active_row)
        if not active_rows:
            raise HTTPException(status_code=404, detail=f"unknown skill: {skill_id}")
        active_version_id = str(active_rows[0][0]) if active_rows[0][0] is not None else None
        rows = store.execute(
            "SELECT version_id, version_number, authored_at, author, change_summary, "
            "status, raw_prose FROM skill_versions WHERE skill_id = $sid "
            "ORDER BY version_number DESC",
            {"sid": skill_id},
        )
        versions = [
            SkillVersion(
                version_id=str(r[0]),
                version_number=int(r[1]),
                authored_at=str(r[2]) if r[2] is not None else None,
                author=str(r[3]),
                change_summary=str(r[4]),
                status=str(r[5]) if r[5] is not None else None,
                raw_prose=str(r[6]),
                is_active=str(r[0]) == active_version_id,
            )
            for r in rows
        ]
        return SkillVersionsResponse(skill_id=skill_id, versions=versions)

    return await asyncio.to_thread(_query)


# ---------------------------------------------------------------------------
# override editor
# ---------------------------------------------------------------------------


class OverrideState(BaseModel):
    skill_id: str
    skill_class: str | None
    active_layer: str  # "project" | "profile" | "default"
    active_profile: str
    paths: dict[str, str | None]  # layer → file path (null when absent)
    raw_prose: str | None  # active layer's prose
    domain_tags: list[str]
    shipped_raw_prose: str | None  # for side-by-side diff
    locked_fields: dict[str, Any]  # product-owned, read-only in the editor
    prose_invariants: list[str]


class OverrideWrite(BaseModel):
    layer: str = "profile"  # "profile" | "project"
    raw_prose: str
    domain_tags: list[str] | None = None
    repo: str | None = None  # required for layer="project"


class OverrideWriteResult(BaseModel):
    status: str
    layer: str
    path: str
    message: str


def _layers_for(skill_id: str, repo: str | None) -> dict[str, Any]:
    from agentalloy.install.subcommands import customize

    cwd = Path(repo) if repo else None
    return customize._resolve_skill_layers(skill_id, None, cwd=cwd)  # pyright: ignore[reportPrivateUsage]


def _load_layer_yaml(path: Path | None) -> dict[str, Any]:
    from agentalloy.install.subcommands import customize

    if path is None:
        return {}
    return customize._load_yaml(path)  # pyright: ignore[reportPrivateUsage]


_LOCKED_FIELDS = (
    "applies_to_phases",
    "exit_gates",
    "signal_keywords",
    "contract_template",
    "applies_when",
)


@router.get(
    "/api/skills/{skill_id}/override",
    response_model=OverrideState,
    summary="Override layers + editable/locked content for a skill",
)
async def get_override(skill_id: str, repo: str | None = Query(default=None)) -> OverrideState:
    def _build() -> OverrideState:
        from agentalloy.install.subcommands import customize
        from agentalloy.signals.invariants import derive_invariants

        layers = _layers_for(skill_id, repo)
        if layers.get("default") is None and layers.get("skill_class") is None:
            raise HTTPException(status_code=404, detail=f"not an overridable skill: {skill_id}")
        layer, active_path = customize._active_layer(layers)  # pyright: ignore[reportPrivateUsage]
        shipped = _load_layer_yaml(layers.get("default"))
        active = _load_layer_yaml(active_path) if active_path else shipped
        invariants: list[str] = []
        # Invariants are advisory in the GET — a derivation failure must not 500.
        with contextlib.suppress(Exception):
            invariants = list(derive_invariants(shipped))
        return OverrideState(
            skill_id=skill_id,
            skill_class=layers.get("skill_class"),
            active_layer=layer,
            active_profile=str(layers.get("active_profile_name")),
            paths={
                key: str(p) if (p := layers.get(key)) is not None else None
                for key in ("project", "profile", "default")
            },
            raw_prose=active.get("raw_prose"),
            domain_tags=list(active.get("domain_tags") or []),
            shipped_raw_prose=shipped.get("raw_prose"),
            locked_fields={k: shipped.get(k) for k in _LOCKED_FIELDS if k in shipped},
            prose_invariants=invariants,
        )

    return await asyncio.to_thread(_build)


@router.put(
    "/api/skills/{skill_id}/override",
    response_model=OverrideWriteResult,
    summary="Validate + save an override in one call",
)
async def put_override(
    skill_id: str,
    body: OverrideWrite,
    x_agentalloy_csrf: Annotated[str | None, Header()] = None,
) -> OverrideWriteResult:
    _require_csrf(x_agentalloy_csrf)
    if body.layer not in ("profile", "project"):
        raise HTTPException(status_code=400, detail="layer must be 'profile' or 'project'")
    if body.layer == "project" and not body.repo:
        raise HTTPException(status_code=400, detail="layer='project' requires 'repo'")

    def _apply() -> OverrideWriteResult:
        from agentalloy.install.subcommands import customize

        layers = _layers_for(skill_id, body.repo)
        skill_class = layers.get("skill_class")
        if skill_class not in _OVERRIDABLE_CLASSES:
            raise HTTPException(
                status_code=400,
                detail=f"only system/workflow skills are customizable (got {skill_class!r})",
            )
        # Compose the override from the shipped default so product-owned fields
        # can never drift — the API accepts prose + tags, nothing else.
        data = dict(_load_layer_yaml(layers.get("default")))
        if not data:
            raise HTTPException(status_code=404, detail=f"no shipped default for {skill_id}")
        data["raw_prose"] = body.raw_prose
        if body.domain_tags is not None:
            data["domain_tags"] = body.domain_tags
        errors = customize._validate_skill_data(data, skill_id)  # pyright: ignore[reportPrivateUsage]
        if errors:
            raise HTTPException(
                status_code=400, detail={"error": "validation_failed", "errors": errors}
            )

        if body.layer == "project":
            target = (
                Path(str(body.repo)) / ".agentalloy" / "skills" / str(skill_class)
            ) / f"{skill_id}.yaml"
        else:
            profile_dir = Path(str(layers["active_profile"].skills_dir))
            target = profile_dir / str(skill_class) / f"{skill_id}.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
        customize._ingest_skill(str(layers["active_profile_name"]), data)  # pyright: ignore[reportPrivateUsage]
        return OverrideWriteResult(
            status="ok",
            layer=body.layer,
            path=str(target),
            message="Override validated, saved, and ingested.",
        )

    return await asyncio.to_thread(_apply)


@router.delete(
    "/api/skills/{skill_id}/override",
    response_model=OverrideWriteResult,
    summary="Delete an override layer (revert to the layer below)",
)
async def delete_override(
    skill_id: str,
    layer: str = Query(default="profile"),
    repo: str | None = Query(default=None),
    x_agentalloy_csrf: Annotated[str | None, Header()] = None,
) -> OverrideWriteResult:
    _require_csrf(x_agentalloy_csrf)
    if layer not in ("profile", "project"):
        raise HTTPException(status_code=400, detail="layer must be 'profile' or 'project'")

    def _apply() -> OverrideWriteResult:
        from agentalloy.install.subcommands import customize

        layers = _layers_for(skill_id, repo)
        path = layers.get(layer)
        if path is None:
            raise HTTPException(status_code=404, detail=f"no {layer} override for {skill_id}")
        Path(path).unlink(missing_ok=True)
        profile_name = str(layers["active_profile_name"])
        # Revert the profile store to whatever layer remains active.
        remaining = _layers_for(skill_id, repo)
        _, still_active = customize._active_layer(remaining)  # pyright: ignore[reportPrivateUsage]
        if still_active is not None and remaining.get("default") != still_active:
            customize._ingest_skill(profile_name, _load_layer_yaml(still_active))  # pyright: ignore[reportPrivateUsage]
        else:
            customize._delete_from_store(profile_name, skill_id)  # pyright: ignore[reportPrivateUsage]
        return OverrideWriteResult(
            status="ok",
            layer=layer,
            path=str(path),
            message="Override deleted; runtime reverts to the layer below.",
        )

    return await asyncio.to_thread(_apply)


# ---------------------------------------------------------------------------
# signal simulator
# ---------------------------------------------------------------------------


class SignalEvaluateRequest(BaseModel):
    repo: str
    prompt: str


class SignalEvaluateResponse(BaseModel):
    should_compose: bool
    phase: str | None
    task: str | None
    domain_tags: list[str]
    announce: bool
    workflow_skill_id: str | None
    current_contract: str | None
    pre_filter_matched: str | None
    gates_met: list[str]
    gates_unmet: list[str]
    qwen_calls: int
    phase_gate_embed_failed: bool
    advisories: list[str]
    banner: str | None
    would_announce: bool


@router.post(
    "/api/signal/evaluate",
    response_model=SignalEvaluateResponse,
    summary="Dry-run the signal layer for a prompt against a repo's current state",
)
async def signal_evaluate(request: Request, body: SignalEvaluateRequest) -> SignalEvaluateResponse:
    """Read-only simulation: no phase write, no banner bump, no cadence markers."""
    from agentalloy.api.proxy_models import ProxyMessage, ProxyRequest
    from agentalloy.api.proxy_signal import evaluate_signal

    repo = Path(body.repo)
    if not repo.is_dir():
        raise HTTPException(status_code=400, detail=f"repo is not a directory: {body.repo}")
    proxy_req = ProxyRequest(
        model="signal-simulator",
        messages=[ProxyMessage(role="user", content=body.prompt)],
    )
    embed_client = getattr(request.app.state, "embed_client", None)
    result = await evaluate_signal(proxy_req, repo, embed_client, mutate=False)
    return SignalEvaluateResponse(
        should_compose=result.should_compose,
        phase=result.phase,
        task=result.task,
        domain_tags=list(result.domain_tags or []),
        announce=result.announce,
        workflow_skill_id=result.workflow_skill_id,
        current_contract=result.current_contract,
        pre_filter_matched=result.pre_filter_matched,
        gates_met=list(result.gates_met),
        gates_unmet=list(result.gates_unmet),
        qwen_calls=result.qwen_calls,
        phase_gate_embed_failed=result.phase_gate_embed_failed,
        advisories=list(result.advisories),
        banner=result.banner,
        would_announce=result.pending_announce is not None,
    )
