"""Fixture loader — reads YAML skill files and seeds the skill store.

Not a product capability. Only used by tests and by local developers to get a
representative runtime store without going through the real ingest flow.

The skill graph (skills / skill_versions / fragments + folded edges) and the
fragment embeddings live together in the unified OverGraph corpus store
(``agentalloy.overgraph``). This loader only writes the skill graph. After
loading fixtures, run ``python -m agentalloy.reembed`` to build the fragment
embeddings from the active fragments.

The fixtures intentionally carry multiple versions per skill (a superseded v1
plus an active v2) and explicit version/fragment ids, so they are written
directly here rather than through ``install.importer`` (which folds every skill
into a single synthetic ``-v1`` active version).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import yaml

from agentalloy.storage.protocols import FragmentRow, SkillRow, SkillStore, SkillVersionRow

logger = logging.getLogger(__name__)

FIXTURES_ROOT = Path(__file__).resolve().parents[3] / "fixtures"


@dataclass(frozen=True)
class LoadSummary:
    skills: int
    versions: int
    fragments: int


def load_fixtures(
    store: SkillStore,
    *,
    fixtures_root: Path = FIXTURES_ROOT,
) -> LoadSummary:
    """Wipe the skill graph tables and re-seed from YAML fixtures.

    To populate the fragment embedding index after loading, run
    ``python -m agentalloy.reembed``.
    """
    store.migrate()  # idempotent; ensures the schema exists before writing
    _wipe(store)
    skills = _read_fixture_files(fixtures_root)
    logger.info("fixtures_load begin files=%d", len(skills))

    created_skills = 0
    created_versions = 0
    created_fragments = 0

    for skill in skills:
        _insert_skill(store, skill)
        created_skills += 1
        versions: list[dict[str, Any]] = skill["versions"]
        for version in versions:
            _insert_version(store, skill["skill_id"], version)
            created_versions += 1
            if version["status"] == "active":
                _link_current_version(store, skill["skill_id"], version["version_id"])
            fragments: list[dict[str, Any]] = version.get("fragments") or []
            for fragment in fragments:
                _insert_fragment(store, version["version_id"], fragment)
                created_fragments += 1

    summary = LoadSummary(
        skills=created_skills,
        versions=created_versions,
        fragments=created_fragments,
    )
    logger.info(
        "fixtures_load ok skills=%d versions=%d fragments=%d",
        summary.skills,
        summary.versions,
        summary.fragments,
    )
    return summary


def _wipe(store: SkillStore) -> None:
    # Clear the skill graph tables (corpus_meta is left intact).
    store.clear_all()


def _read_fixture_files(root: Path) -> list[dict[str, Any]]:
    if not root.exists():
        raise FileNotFoundError(f"fixtures directory not found: {root}")
    files = sorted([*root.glob("domain/*.yaml"), *root.glob("system/*.yaml")])
    out: list[dict[str, Any]] = []
    for f in files:
        raw: Any = yaml.safe_load(f.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"invalid fixture (expected mapping): {f}")
        out.append(cast("dict[str, Any]", raw))
    return out


def _insert_skill(store: SkillStore, skill: dict[str, Any]) -> None:
    store.insert_skill(
        SkillRow(
            skill_id=skill["skill_id"],
            canonical_name=skill["canonical_name"],
            category=skill["category"],
            skill_class=skill["skill_class"],
            domain_tags=skill.get("domain_tags") or [],
            deprecated=bool(skill.get("deprecated", False)),
            superseded_by=None,
            always_apply=bool(skill.get("always_apply", False)),
            phase_scope=skill.get("phase_scope") or None,
            category_scope=skill.get("category_scope") or None,
            tier=None,
            description=None,
            current_version_id="",  # linked after the active version is inserted
        )
    )


def _insert_version(store: SkillStore, skill_id: str, version: dict[str, Any]) -> None:
    authored_at = version.get("authored_at")
    if isinstance(authored_at, str):
        authored_dt = datetime.fromisoformat(authored_at.replace("Z", "+00:00"))
    elif isinstance(authored_at, datetime):
        authored_dt = authored_at
    else:
        raise ValueError(f"invalid authored_at on version {version.get('version_id')}")

    store.insert_version(
        SkillVersionRow(
            version_id=version["version_id"],
            skill_id=skill_id,
            version_number=int(version["version_number"]),
            authored_at=authored_dt,
            author=version.get("author", "fixture-seed"),
            change_summary=version.get("change_summary", ""),
            status=version["status"],
            raw_prose=version.get("raw_prose", ""),
        )
    )


def _link_current_version(store: SkillStore, skill_id: str, version_id: str) -> None:
    # The old CURRENT_VERSION edge is folded into skills.current_version_id.
    # Re-insert the skill row with the current_version_id populated.
    existing = store.get_skill(skill_id)
    if existing is not None:
        store.insert_skill(
            SkillRow(
                skill_id=existing.skill_id,
                canonical_name=existing.canonical_name,
                category=existing.category,
                skill_class=existing.skill_class,
                domain_tags=existing.domain_tags,
                deprecated=existing.deprecated,
                superseded_by=existing.superseded_by,
                always_apply=existing.always_apply,
                phase_scope=existing.phase_scope,
                category_scope=existing.category_scope,
                tier=existing.tier,
                description=existing.description,
                current_version_id=version_id,
            )
        )


def _insert_fragment(
    store: SkillStore,
    version_id: str,
    fragment: dict[str, Any],
) -> None:
    store.insert_fragment(
        FragmentRow(
            fragment_id=fragment["fragment_id"],
            version_id=version_id,
            fragment_type=fragment["fragment_type"],
            sequence=int(fragment["sequence"]),
            content=fragment["content"],
        )
    )
