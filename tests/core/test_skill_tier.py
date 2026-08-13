"""Tests for agentalloy.skill_tier.resolve_skill_tier."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from agentalloy.skill_tier import resolve_skill_tier


def _write_pack_yaml(directory: Path, content: dict[str, Any]) -> None:
    (directory / "pack.yaml").write_text(yaml.dump(content), encoding="utf-8")


def _write_skill_yaml(directory: Path, name: str = "skill.yaml") -> Path:
    skill_path = directory / name
    skill_path.write_text("skill_id: test-skill\n", encoding="utf-8")
    return skill_path


class TestResolveSkillTier:
    def test_pack_yaml_with_tier_returns_tier(self, tmp_path: Path) -> None:
        """Skill YAML in a pack dir that has pack.yaml with tier: foundation."""
        _write_pack_yaml(tmp_path, {"name": "core", "tier": "foundation"})
        skill_path = _write_skill_yaml(tmp_path)

        tier, source = resolve_skill_tier(skill_path)

        assert tier == "foundation"
        assert source == "pack.yaml"

    def test_pack_yaml_missing_tier_returns_none_with_missing_label(self, tmp_path: Path) -> None:
        """Skill YAML in a dir with pack.yaml but no tier field."""
        _write_pack_yaml(tmp_path, {"name": "core"})
        skill_path = _write_skill_yaml(tmp_path)

        tier, source = resolve_skill_tier(skill_path)

        assert tier is None
        assert source == "pack.yaml:missing"

    def test_no_pack_yaml_returns_not_found(self, tmp_path: Path) -> None:
        """Skill YAML with no pack.yaml anywhere in the walk."""
        skill_path = _write_skill_yaml(tmp_path)

        tier, source = resolve_skill_tier(skill_path)

        assert tier is None
        assert source == "not_found"

    def test_accepts_string_path(self, tmp_path: Path) -> None:
        """Passing a string path (not Path) works correctly."""
        _write_pack_yaml(tmp_path, {"name": "language", "tier": "language"})
        skill_path = _write_skill_yaml(tmp_path)

        tier, source = resolve_skill_tier(str(skill_path))

        assert tier == "language"
        assert source == "pack.yaml"

    def test_walks_up_to_parent_directory(self, tmp_path: Path) -> None:
        """pack.yaml in parent dir is found when skill is in a subdirectory."""
        _write_pack_yaml(tmp_path, {"name": "core", "tier": "foundation"})
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        skill_path = _write_skill_yaml(subdir)

        tier, source = resolve_skill_tier(skill_path)

        assert tier == "foundation"
        assert source == "pack.yaml"

    def test_corrupt_pack_yaml_returns_parse_error(self, tmp_path: Path) -> None:
        """A YAML parse error in pack.yaml returns (None, 'pack.yaml:parse_error')."""
        (tmp_path / "pack.yaml").write_text(":\tinvalid: yaml: [[", encoding="utf-8")
        skill_path = _write_skill_yaml(tmp_path)

        tier, source = resolve_skill_tier(skill_path)

        assert tier is None
        assert source == "pack.yaml:parse_error"

    def test_empty_pack_yaml_returns_missing(self, tmp_path: Path) -> None:
        """An empty pack.yaml has no tier key → (None, 'pack.yaml:missing')."""
        (tmp_path / "pack.yaml").write_text("", encoding="utf-8")
        skill_path = _write_skill_yaml(tmp_path)

        tier, source = resolve_skill_tier(skill_path)

        assert tier is None
        assert source == "pack.yaml:missing"

    def test_non_string_tier_returns_missing(self, tmp_path: Path) -> None:
        """A non-string tier value (null, number) returns (None, 'pack.yaml:missing')."""
        (tmp_path / "pack.yaml").write_text("tier: 123\n", encoding="utf-8")
        skill_path = _write_skill_yaml(tmp_path)

        tier, source = resolve_skill_tier(skill_path)

        assert tier is None
        assert source == "pack.yaml:missing"
