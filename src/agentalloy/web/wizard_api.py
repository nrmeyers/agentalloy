"""Web UI custom-skill creation wizard — the human-driven twin of the add-skill lane.

Same rails as the lane, driven by clicks instead of prose: scaffold
(``new-skill-pack``) → draft (edit the skill YAML) → validate
(``validate-pack``, strict) → approve + install (``install-pack``: strict lint
+ dedup gate + reembed).

Packs are confined to ``<repo>/.agentalloy/custom-skills/<pack>`` — the same
load-bearing location the lane's exit gate and ``approve add-skill`` glob.
When the repo is sitting in the ``add-skill`` phase, install also records the
human approval marker (``run_approve``) so the lane's gate releases and the
phase auto-advances back to intake; outside the lane it installs directly —
the click is the approval.

All mutations require the ``X-AgentAlloy-CSRF: 1`` header.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Annotated, Any

import yaml
from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel

from agentalloy.web.config_api import _require_csrf

router = APIRouter()

_PACK_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
_FILE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}\.yaml$")


def _pack_dir(repo: str, pack: str) -> Path:
    root = Path(repo)
    if not root.is_dir():
        raise HTTPException(status_code=400, detail=f"repo is not a directory: {repo}")
    if not _PACK_NAME_RE.match(pack):
        raise HTTPException(status_code=400, detail=f"invalid pack name: {pack!r}")
    return root / ".agentalloy" / "custom-skills" / pack


class ScaffoldRequest(BaseModel):
    repo: str
    pack: str
    skill_id: str
    skill_class: str = "domain"
    canonical_name: str | None = None


@router.post("/api/wizard/scaffold", summary="Scaffold a custom skill pack (new-skill-pack)")
async def scaffold(
    body: ScaffoldRequest,
    x_agentalloy_csrf: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _require_csrf(x_agentalloy_csrf)
    pack_dir = _pack_dir(body.repo, body.pack)

    def _run() -> dict[str, Any]:
        from agentalloy.install.subcommands.new_skill_pack import new_skill_pack

        result = new_skill_pack(
            pack_dir,
            skill_id=body.skill_id,
            skill_class=body.skill_class,
            canonical_name=body.canonical_name,
            pack_name=body.pack,
        )
        if result.get("error"):
            raise HTTPException(
                status_code=400,
                detail={"error": str(result.get("action")), "detail": str(result.get("error"))},
            )
        skill_file = pack_dir / f"{body.skill_id}.yaml"
        result["skill_file"] = skill_file.name
        result["skill_yaml"] = skill_file.read_text(encoding="utf-8")
        return result

    return await asyncio.to_thread(_run)


class PackFile(BaseModel):
    name: str
    content: str


class PackContents(BaseModel):
    pack: str
    pack_dir: str
    exists: bool
    files: list[PackFile]


@router.get("/api/wizard/pack", response_model=PackContents, summary="Read a custom pack's files")
async def read_pack(repo: str = Query(...), pack: str = Query(...)) -> PackContents:
    pack_dir = _pack_dir(repo, pack)

    def _read() -> PackContents:
        if not pack_dir.is_dir():
            return PackContents(pack=pack, pack_dir=str(pack_dir), exists=False, files=[])
        files = [
            PackFile(name=p.name, content=p.read_text(encoding="utf-8"))
            for p in sorted(pack_dir.glob("*.yaml"))
        ]
        return PackContents(pack=pack, pack_dir=str(pack_dir), exists=True, files=files)

    return await asyncio.to_thread(_read)


class FileWrite(BaseModel):
    repo: str
    pack: str
    file: str
    content: str


@router.put("/api/wizard/file", summary="Write a file inside a custom pack (draft step)")
async def write_file(
    body: FileWrite,
    x_agentalloy_csrf: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _require_csrf(x_agentalloy_csrf)
    pack_dir = _pack_dir(body.repo, body.pack)
    if not _FILE_RE.match(body.file):
        raise HTTPException(status_code=400, detail=f"invalid file name: {body.file!r}")
    if not pack_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"pack not scaffolded: {body.pack}")
    try:
        parsed = yaml.safe_load(body.content)
    except yaml.YAMLError as exc:
        raise HTTPException(
            status_code=400, detail={"error": "invalid_yaml", "detail": str(exc)}
        ) from exc
    if not isinstance(parsed, dict):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_yaml", "detail": "content must be a YAML mapping"},
        )
    target = pack_dir / body.file
    await asyncio.to_thread(target.write_text, body.content, "utf-8")
    return {"status": "ok", "path": str(target)}


class PackRef(BaseModel):
    repo: str
    pack: str


@router.post("/api/wizard/validate", summary="Strict schema+lint dry-run (validate-pack)")
async def validate(
    body: PackRef,
    x_agentalloy_csrf: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _require_csrf(x_agentalloy_csrf)
    pack_dir = _pack_dir(body.repo, body.pack)
    if not pack_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"pack not scaffolded: {body.pack}")

    def _run() -> dict[str, Any]:
        from agentalloy.install.subcommands.validate_pack import validate_pack

        return validate_pack(pack_dir, strict=True)

    return await asyncio.to_thread(_run)


class InstallRequest(BaseModel):
    repo: str
    pack: str
    approver: str | None = None
    allow_duplicates: bool = False


@router.post(
    "/api/wizard/install",
    summary="Approve (when in the add-skill lane) and install the pack",
)
async def install(
    body: InstallRequest,
    x_agentalloy_csrf: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _require_csrf(x_agentalloy_csrf)
    root = Path(body.repo)
    pack_dir = _pack_dir(body.repo, body.pack)
    if not pack_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"pack not scaffolded: {body.pack}")

    def _run() -> dict[str, Any]:
        from agentalloy.install.subcommands.approve import run_approve
        from agentalloy.install.subcommands.install_pack import install_local_pack
        from agentalloy.install.subcommands.status import (
            _repo_phase,  # pyright: ignore[reportPrivateUsage]
        )

        approval: dict[str, Any] | None = None
        if _repo_phase(body.repo) == "add-skill":
            # Complete the lane: the click is the human sign-off. run_approve
            # writes the marker and auto-advances back to intake.
            approval = run_approve("add-skill", root=root, approver=body.approver)
            if not approval.get("ok"):
                raise HTTPException(
                    status_code=409,
                    detail={"error": "approve_refused", "detail": approval.get("error")},
                )
        result = install_local_pack(
            pack_dir,
            root=root,
            no_restart=True,
            strict=True,
            allow_duplicates=body.allow_duplicates,
        )
        return {"approval": approval, "install": result}

    return await asyncio.to_thread(_run)
