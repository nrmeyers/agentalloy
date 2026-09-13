"""Read-skill inspection endpoint — GET /skills/{skill_id} (NXS-774)."""

from __future__ import annotations

import asyncio
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from agentalloy.reads.active import (
    get_active_fragments_for_skill,
    get_active_skill_by_id,
    get_active_version_by_id,
)
from agentalloy.storage.protocols import SkillStore

router = APIRouter()


class FragmentDetail(BaseModel):
    fragment_id: str
    fragment_type: str
    sequence: int
    content: str


class ActiveVersionDetail(BaseModel):
    version_id: str
    version_number: int
    authored_at: datetime
    author: str
    change_summary: str
    raw_prose: str


class SkillInspectionResponse(BaseModel):
    skill_id: str
    canonical_name: str
    category: str
    skill_class: str
    is_active: bool
    active_version: ActiveVersionDetail
    fragments: list[FragmentDetail]


def get_skill_store() -> SkillStore:
    raise RuntimeError("get_skill_store must be bound during app lifespan; no default available")


@router.get(
    "/skills/{skill_id}",
    response_model=SkillInspectionResponse,
    responses={404: {"description": "No active skill with the given id"}},
    summary="Inspect active skill identity, version, and fragments",
)
async def inspect_skill(
    skill_id: str,
    store: SkillStore = Depends(get_skill_store),
) -> SkillInspectionResponse:
    skill = await asyncio.to_thread(get_active_skill_by_id, store, skill_id)
    if skill is None:
        raise HTTPException(
            status_code=404,
            detail=f"skill {skill_id!r} not found or has no active version",
        )

    fragments_raw = await asyncio.to_thread(get_active_fragments_for_skill, store, skill_id)
    version_detail = await asyncio.to_thread(_fetch_version_detail, store, skill.active_version_id)

    return SkillInspectionResponse(
        skill_id=skill.skill_id,
        canonical_name=skill.canonical_name,
        category=skill.category,
        skill_class=skill.skill_class,
        is_active=True,
        active_version=version_detail,
        fragments=[
            FragmentDetail(
                fragment_id=f.fragment_id,
                fragment_type=f.fragment_type,
                sequence=f.sequence,
                content=f.content,
            )
            for f in fragments_raw
        ],
    )


def _fetch_version_detail(store: SkillStore, version_id: str) -> ActiveVersionDetail:
    data = get_active_version_by_id(store, version_id)
    return ActiveVersionDetail(
        version_id=data["version_id"],
        version_number=data["version_number"],
        authored_at=data["authored_at"],
        author=data["author"],
        change_summary=data["change_summary"],
        raw_prose=data["raw_prose"],
    )
