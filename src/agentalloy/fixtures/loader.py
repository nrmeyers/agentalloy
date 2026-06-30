"""Fixture loader — reads YAML skill files and seeds the skill store.

Not a product capability. Only used by tests and by local developers to get a
representative runtime store without going through the real ingest flow.

In v5 the skill graph (skills / skill_versions / fragments + folded edges) lives
in DuckDB ``agentalloy.duck`` (the ``SkillStore``); embeddings live in the Lance
``fragments`` dataset. This loader only writes the skill graph. After loading
fixtures, run ``python -m agentalloy.reembed`` to build the Lance fragments
dataset from the active fragments.

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

from agentalloy.storage.protocols import SkillStore

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

    To populate the Lance ``fragments`` dataset after loading, run
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
        skills=created_skills, versions=created_versions, fragments=created_fragments
    )
    logger.info(
        "fixtures_load ok skills=%d versions=%d fragments=%d",
        summary.skills,
        summary.versions,
        summary.fragments,
    )
    return summary


def _wipe(store: SkillStore) -> None:
    # Clear the skill graph tables (corpus_meta is left intact). No FK cascade is
    # declared, so order is cosmetic; we delete children before parents anyway.
    store.execute("DELETE FROM fragments")
    store.execute("DELETE FROM skill_dependencies")
    store.execute("DELETE FROM skill_versions")
    store.execute("DELETE FROM skills")


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
    store.execute(
        "INSERT INTO skills (skill_id, canonical_name, category, skill_class, "
        "domain_tags, deprecated, always_apply, phase_scope, category_scope) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            skill["skill_id"],
            skill["canonical_name"],
            skill["category"],
            skill["skill_class"],
            skill.get("domain_tags") or [],
            bool(skill.get("deprecated", False)),
            bool(skill.get("always_apply", False)),
            skill.get("phase_scope") or [],
            skill.get("category_scope") or [],
        ],
    )


def _insert_version(store: SkillStore, skill_id: str, version: dict[str, Any]) -> None:
    authored_at = version.get("authored_at")
    if isinstance(authored_at, str):
        authored_dt = datetime.fromisoformat(authored_at.replace("Z", "+00:00"))
    elif isinstance(authored_at, datetime):
        authored_dt = authored_at
    else:
        raise ValueError(f"invalid authored_at on version {version.get('version_id')}")

    store.execute(
        "INSERT INTO skill_versions (version_id, skill_id, version_number, authored_at, "
        "author, change_summary, status, raw_prose) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            version["version_id"],
            skill_id,
            int(version["version_number"]),
            authored_dt,
            version.get("author", "fixture-seed"),
            version.get("change_summary", ""),
            version["status"],
            version.get("raw_prose", ""),
        ],
    )


def _link_current_version(store: SkillStore, skill_id: str, version_id: str) -> None:
    # The old CURRENT_VERSION edge is folded into skills.current_version_id.
    store.execute(
        "UPDATE skills SET current_version_id = ? WHERE skill_id = ?",
        [version_id, skill_id],
    )


def _insert_fragment(
    store: SkillStore,
    version_id: str,
    fragment: dict[str, Any],
) -> None:
    # The old DECOMPOSES_TO edge is folded into fragments.version_id.
    store.execute(
        "INSERT INTO fragments (fragment_id, version_id, fragment_type, sequence, content) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            fragment["fragment_id"],
            version_id,
            fragment["fragment_type"],
            int(fragment["sequence"]),
            fragment["content"],
        ],
    )
