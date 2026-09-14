"""Import v1 skill corpus (355 skills / 41 packs) into v2 SkillEngine.

Reads YAML skill files from v1's _packs/ directory, maps them to v2's Skill
dataclass, and provides the full corpus for phase-aware steering.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from agentalloy.skill_engine import Skill, SkillFragment
from agentalloy.state_store import PHASE_ORDER

# Default v1 packs location (bundled in the wheel as package data)
DEFAULT_V1_PACKS = Path(__file__).resolve().parent / "_packs"

# v1 phase names that map to v2 phases
VALID_PHASES = {"spec", "design", "plan", "build", "qa", "ship", "intake"}

# v1 skill classes
VALID_SKILL_CLASSES = {"system", "workflow", "domain"}


@dataclass
class PackMeta:
    """Metadata from a pack.yaml."""

    name: str
    version: str = "0.0.0"
    tier: str = "language"
    description: str = ""
    always_install: bool = False
    depends_on: list[str] = field(default_factory=list)
    skill_count: int = 0


def _normalize_phases(raw: Any) -> list[str]:
    """Convert v1 phase_scope / applies_to_phases to v2 phase list."""
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if isinstance(raw, list):
        return [str(p).strip().lower() for p in raw if str(p).strip().lower() in VALID_PHASES]
    return []


def _skill_from_yaml(data: dict[str, Any], pack_name: str) -> Skill:
    """Map a v1 skill YAML dict to v2 Skill dataclass."""
    skill_id = str(data.get("skill_id", ""))
    canonical_name = str(data.get("canonical_name", skill_id))
    description = str(data.get("description", ""))

    # Phase scope: v1 uses phase_scope OR applies_to_phases
    phases = _normalize_phases(data.get("phase_scope"))
    if not phases:
        phases = _normalize_phases(data.get("applies_to_phases"))
    # Default to all phases if none specified
    if not phases:
        phases = list(PHASE_ORDER)

    # Domain tags
    domain_tags = data.get("domain_tags", [])
    if not isinstance(domain_tags, list):
        domain_tags = []
    domain_tags = [str(t) for t in domain_tags]

    # Skill class
    skill_class = str(data.get("skill_class", "domain"))
    if skill_class not in VALID_SKILL_CLASSES:
        skill_class = "domain"

    # Fragments: preserved as-is for fragment-native selection.
    # v1 sequences are 1-based (min observed = 1); head-reserve in the
    # assembler uses each skill's minimum sequence, so base doesn't matter.
    fragments: list[SkillFragment] = []
    raw_frags = data.get("fragments", [])
    if isinstance(raw_frags, list):
        for i, frag in enumerate(raw_frags):
            if not isinstance(frag, dict) or "content" not in frag:
                continue
            try:
                sequence = int(frag.get("sequence", i))
            except (TypeError, ValueError):
                sequence = i
            fragments.append(
                SkillFragment(
                    fragment_id=f"{skill_id}-f{sequence}",
                    fragment_type=str(frag.get("fragment_type", "execution")),
                    sequence=sequence,
                    content=str(frag["content"]),
                )
            )

    # Body: prefer raw_prose, fall back to concatenating fragments
    body = str(data.get("raw_prose", ""))
    if not body and fragments:
        body = "\n\n".join(f.content for f in fragments)

    return Skill(
        id=skill_id,
        name=canonical_name,
        description=description,
        body=body,
        phases=phases,
        domain_tags=domain_tags,
        skill_class=skill_class,
        pack=pack_name,
        fragments=fragments,
    )


def load_pack_meta(pack_dir: Path) -> PackMeta:
    """Load pack.yaml metadata."""
    pack_yaml = pack_dir / "pack.yaml"
    if not pack_yaml.exists():
        return PackMeta(name=pack_dir.name)

    data = yaml.safe_load(pack_yaml.read_text(encoding="utf-8")) or {}
    skills_list = data.get("skills", [])
    return PackMeta(
        name=str(data.get("name", pack_dir.name)),
        version=str(data.get("version", "0.0.0")),
        tier=str(data.get("tier", "language")),
        description=str(data.get("description", "")),
        always_install=bool(data.get("always_install", False)),
        depends_on=data.get("depends_on", []) or [],
        skill_count=len(skills_list) if isinstance(skills_list, list) else 0,
    )


def import_pack(pack_dir: Path) -> tuple[PackMeta, list[Skill]]:
    """Import all skills from a single pack directory."""
    meta = load_pack_meta(pack_dir)
    skills: list[Skill] = []

    for yaml_file in sorted(pack_dir.glob("*.yaml")):
        if yaml_file.name == "pack.yaml":
            continue
        try:
            data = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or "skill_id" not in data:
                continue
            skill = _skill_from_yaml(data, meta.name)
            skills.append(skill)
        except (yaml.YAMLError, OSError):
            continue

    meta.skill_count = len(skills)
    return meta, skills


def import_corpus(packs_dir: Path | None = None) -> tuple[list[Skill], dict[str, PackMeta]]:
    """Import the full v1 corpus.

    Returns:
        (skills, packs) — all 355 skills and their pack metadata.
    """
    if packs_dir is None:
        packs_dir = DEFAULT_V1_PACKS

    if not packs_dir.exists():
        return [], {}

    all_skills: list[Skill] = []
    all_packs: dict[str, PackMeta] = {}

    for pack_dir in sorted(packs_dir.iterdir()):
        if not pack_dir.is_dir():
            continue
        meta, skills = import_pack(pack_dir)
        all_packs[meta.name] = meta
        all_skills.extend(skills)

    return all_skills, all_packs


def corpus_summary(skills: list[Skill], packs: dict[str, PackMeta]) -> dict[str, Any]:
    """Return a summary of the imported corpus."""
    phase_counts: dict[str, int] = {}
    class_counts: dict[str, int] = {}
    for s in skills:
        for p in s.phases:
            phase_counts[p] = phase_counts.get(p, 0) + 1
        class_counts[s.skill_class] = class_counts.get(s.skill_class, 0) + 1

    return {
        "total_skills": len(skills),
        "total_packs": len(packs),
        "by_phase": phase_counts,
        "by_class": class_counts,
        "always_install_packs": [name for name, meta in packs.items() if meta.always_install],
    }
