"""Corpus importer tests — verify v1 skill import (M2)."""

from pathlib import Path

import yaml

from agentalloy.corpus_importer import (
    _normalize_phases,
    _skill_from_yaml,
    import_corpus,
    import_pack,
    load_pack_meta,
)


def test_normalize_phases() -> None:
    """Phase normalization handles various formats."""
    assert _normalize_phases(["build", "qa"]) == ["build", "qa"]
    assert _normalize_phases("build") == ["build"]
    assert _normalize_phases(None) == []
    assert _normalize_phases([]) == []
    assert _normalize_phases(["build", "invalid"]) == ["build"]
    assert _normalize_phases(["SPEC", "Design"]) == ["spec", "design"]


def test_skill_from_yaml_basic() -> None:
    """Map a v1 skill YAML dict to v2 Skill."""
    data = {
        "skill_id": "python-dataclasses",
        "canonical_name": "Python Dataclasses",
        "description": "Define data models without boilerplate",
        "category": "engineering",
        "skill_class": "domain",
        "domain_tags": ["python", "dataclass"],
        "phase_scope": ["build"],
        "raw_prose": "# Python Dataclasses\n\nContent here.",
    }
    skill = _skill_from_yaml(data, "python")

    assert skill.id == "python-dataclasses"
    assert skill.name == "Python Dataclasses"
    assert skill.description == "Define data models without boilerplate"
    assert skill.phases == ["build"]
    assert skill.domain_tags == ["python", "dataclass"]
    assert skill.skill_class == "domain"
    assert skill.pack == "python"
    assert "Content here." in skill.body


def test_skill_from_yaml_applies_to_phases() -> None:
    """Skills using applies_to_phases instead of phase_scope."""
    data = {
        "skill_id": "sdd-spec",
        "canonical_name": "SDD Spec",
        "description": "Spec and scoping",
        "skill_class": "workflow",
        "domain_tags": ["sdd"],
        "applies_to_phases": ["spec"],
        "raw_prose": "Spec content",
    }
    skill = _skill_from_yaml(data, "sdd")
    assert skill.phases == ["spec"]
    assert skill.skill_class == "workflow"


def test_skill_from_yaml_no_phases_defaults_all() -> None:
    """Skills with no phase scope default to all phases."""
    data = {
        "skill_id": "general-skill",
        "canonical_name": "General",
        "description": "General skill",
        "skill_class": "domain",
        "domain_tags": [],
        "raw_prose": "General content",
    }
    skill = _skill_from_yaml(data, "core")
    assert len(skill.phases) == 7  # all v2 phases (intake → ship)


def test_skill_from_yaml_fragments_fallback() -> None:
    """When raw_prose is empty, concatenate fragments — and preserve them."""
    data = {
        "skill_id": "frag-skill",
        "canonical_name": "Fragmented",
        "description": "Skill with fragments",
        "skill_class": "domain",
        "domain_tags": [],
        "phase_scope": ["build"],
        "raw_prose": "",
        "fragments": [
            {"fragment_type": "setup", "sequence": 1, "content": "Part 1"},
            {"fragment_type": "execution", "sequence": 2, "content": "Part 2"},
        ],
    }
    skill = _skill_from_yaml(data, "test")
    assert "Part 1" in skill.body
    assert "Part 2" in skill.body
    # Fragment model preserved (selection unit is skill + types)
    assert len(skill.fragments) == 2
    assert skill.fragments[0].fragment_id == "frag-skill-f1"
    assert skill.fragments[0].fragment_type == "setup"
    assert skill.fragments[0].sequence == 1
    assert skill.fragments[0].content == "Part 1"
    assert skill.fragments[1].fragment_id == "frag-skill-f2"
    assert skill.fragments[1].fragment_type == "execution"
    assert skill.fragments[1].sequence == 2


def test_skill_from_yaml_raw_prose_and_fragments() -> None:
    """raw_prose wins for body; fragments are still preserved for assembly."""
    data = {
        "skill_id": "both-skill",
        "canonical_name": "Both",
        "description": "Has prose and fragments",
        "skill_class": "domain",
        "domain_tags": [],
        "phase_scope": ["build"],
        "raw_prose": "The prose body.",
        "fragments": [
            {"fragment_type": "execution", "sequence": 1, "content": "Frag 1"},
            {"fragment_type": "example", "sequence": 2, "content": "Frag 2"},
        ],
    }
    skill = _skill_from_yaml(data, "test")
    assert skill.body == "The prose body."
    assert len(skill.fragments) == 2
    assert [f.fragment_type for f in skill.fragments] == ["execution", "example"]


def test_load_pack_meta(tmp_path: Path) -> None:
    """Load pack.yaml metadata."""
    pack_dir = tmp_path / "test-pack"
    pack_dir.mkdir()
    (pack_dir / "pack.yaml").write_text(
        yaml.dump(
            {
                "name": "test-pack",
                "version": "1.0.0",
                "tier": "language",
                "description": "Test pack",
                "always_install": True,
                "depends_on": ["core"],
                "skills": [
                    {"skill_id": "s1", "file": "s1.yaml", "fragment_count": 5},
                    {"skill_id": "s2", "file": "s2.yaml", "fragment_count": 3},
                ],
            }
        )
    )

    meta = load_pack_meta(pack_dir)
    assert meta.name == "test-pack"
    assert meta.version == "1.0.0"
    assert meta.tier == "language"
    assert meta.always_install is True
    assert meta.depends_on == ["core"]
    assert meta.skill_count == 2


def test_import_pack(tmp_path: Path) -> None:
    """Import skills from a pack directory."""
    pack_dir = tmp_path / "my-pack"
    pack_dir.mkdir()

    # pack.yaml
    (pack_dir / "pack.yaml").write_text(
        yaml.dump({"name": "my-pack", "version": "1.0.0", "skills": []})
    )

    # Two skill files
    for i in range(2):
        (pack_dir / f"skill-{i}.yaml").write_text(
            yaml.dump(
                {
                    "skill_id": f"skill-{i}",
                    "canonical_name": f"Skill {i}",
                    "description": f"Description {i}",
                    "skill_class": "domain",
                    "domain_tags": [f"tag-{i}"],
                    "phase_scope": ["build"],
                    "raw_prose": f"Content {i}",
                }
            )
        )

    meta, skills = import_pack(pack_dir)
    assert meta.name == "my-pack"
    assert len(skills) == 2
    assert skills[0].id == "skill-0"
    assert skills[1].id == "skill-1"


def test_import_corpus_v1() -> None:
    """Import the full v1 corpus (355 skills / 41 packs)."""
    skills, packs = import_corpus()

    # v1 has 355 skills across 41 packs
    assert len(skills) == 355, f"Expected 355 skills, got {len(skills)}"
    assert len(packs) == 41, f"Expected 41 packs, got {len(packs)}"

    # Verify some known packs exist
    assert "python" in packs
    assert "core" in packs
    assert "sdd" in packs
    assert "rust" in packs

    # Verify skills have required fields
    for skill in skills[:10]:
        assert skill.id
        assert skill.name
        assert skill.phases
        assert isinstance(skill.domain_tags, list)


def test_import_corpus_nonexistent_dir() -> None:
    """Import from nonexistent directory returns empty."""
    skills, packs = import_corpus(Path("/nonexistent/path"))
    assert skills == []
    assert packs == {}


def test_skill_engine_load_full_corpus() -> None:
    """SkillEngine can load the full v1 corpus."""
    from agentalloy.skill_engine import SkillEngine

    engine = SkillEngine()
    builtin_count = len(engine.list_skills())
    assert builtin_count == 32  # built-in only

    count = engine.load_full_corpus()
    assert count == 355
    assert engine.corpus_loaded is True
    # 32 built-in + 355 imported (some may overlap by id)
    assert len(engine.list_skills()) >= 355


def test_corpus_summary() -> None:
    """Corpus summary reports correct stats."""
    from agentalloy.corpus_importer import corpus_summary

    skills, packs = import_corpus()
    summary = corpus_summary(skills, packs)

    assert summary["total_skills"] == 355
    assert summary["total_packs"] == 41
    assert "build" in summary["by_phase"]
    assert "domain" in summary["by_class"]
    assert "core" in summary["always_install_packs"]
