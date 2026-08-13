# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
"""Unit tests for the ``new-skill-pack`` subcommand (scaffolding).

Covers: fresh pack.yaml + skill YAML creation, appending a second skill to
an existing pack.yaml without clobbering prior metadata/entries, refusing to
overwrite an existing skill YAML, and — the most important gate — that the
generated domain-skill scaffold passes ``pack_validation.validate_pack_skills``
under ``strict=True`` with zero errors (this is the entire point of the
feature: a fresh scaffold's first ``validate-pack`` run must be clean).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from agentalloy.ingest import _validate_gate_spec
from agentalloy.install.subcommands import new_skill_pack as nsp
from agentalloy.pack_validation import validate_pack_skills

# ---------------------------------------------------------------------------
# Core scaffolding — fresh pack_dir
# ---------------------------------------------------------------------------


class TestFreshPack:
    def test_creates_pack_yaml_and_skill_yaml(self, tmp_path: Path) -> None:
        pack_dir = tmp_path / "demo-pack"
        result = nsp.new_skill_pack(pack_dir, skill_id="demo-skill", skill_class="domain")

        assert result["action"] == "scaffolded"
        assert (pack_dir / "pack.yaml").is_file()
        assert (pack_dir / "demo-skill.yaml").is_file()

        manifest = yaml.safe_load((pack_dir / "pack.yaml").read_text(encoding="utf-8"))
        assert manifest["name"] == "demo-pack"
        assert manifest["version"] == "0.1.0"
        assert manifest["tier"] == "domain"
        assert manifest["author"] == "local"
        assert manifest["embed_model"] == "nomic-embed-text-v1.5"
        assert manifest["embedding_dim"] == 768
        assert manifest["license"] == "MIT"
        assert manifest["always_install"] is False
        assert manifest["skills"] == [
            {"skill_id": "demo-skill", "file": "demo-skill.yaml", "fragment_count": 3}
        ]

    def test_pack_name_flag_used_for_new_manifest(self, tmp_path: Path) -> None:
        pack_dir = tmp_path / "some-dir"
        result = nsp.new_skill_pack(
            pack_dir, skill_id="demo-skill", skill_class="domain", pack_name="custom-name"
        )
        assert result["pack_name"] == "custom-name"
        manifest = yaml.safe_load((pack_dir / "pack.yaml").read_text(encoding="utf-8"))
        assert manifest["name"] == "custom-name"

    def test_canonical_name_derived_from_skill_id(self, tmp_path: Path) -> None:
        pack_dir = tmp_path / "demo-pack"
        result = nsp.new_skill_pack(pack_dir, skill_id="foo-bar", skill_class="domain")
        assert result["canonical_name"] == "Foo Bar"

    def test_canonical_name_flag_overrides_derivation(self, tmp_path: Path) -> None:
        pack_dir = tmp_path / "demo-pack"
        result = nsp.new_skill_pack(
            pack_dir, skill_id="foo-bar", skill_class="domain", canonical_name="Custom Title"
        )
        assert result["canonical_name"] == "Custom Title"
        skill_doc = yaml.safe_load((pack_dir / "foo-bar.yaml").read_text(encoding="utf-8"))
        assert skill_doc["canonical_name"] == "Custom Title"


# ---------------------------------------------------------------------------
# Appending a second skill to an existing pack.yaml
# ---------------------------------------------------------------------------


class TestAppendToExistingPack:
    def test_second_skill_appends_without_clobbering(self, tmp_path: Path) -> None:
        pack_dir = tmp_path / "demo-pack"
        nsp.new_skill_pack(pack_dir, skill_id="first-skill", skill_class="domain")

        manifest_before = yaml.safe_load((pack_dir / "pack.yaml").read_text(encoding="utf-8"))
        # Simulate the user having hand-edited pack-level metadata.
        manifest_before["description"] = "hand-edited description"
        manifest_before["homepage"] = "https://example.com/custom"
        (pack_dir / "pack.yaml").write_text(
            yaml.safe_dump(manifest_before, sort_keys=False), encoding="utf-8"
        )

        result = nsp.new_skill_pack(pack_dir, skill_id="second-skill", skill_class="domain")
        assert result["action"] == "scaffolded"

        manifest_after = yaml.safe_load((pack_dir / "pack.yaml").read_text(encoding="utf-8"))
        # Prior metadata preserved verbatim.
        assert manifest_after["description"] == "hand-edited description"
        assert manifest_after["homepage"] == "https://example.com/custom"
        assert manifest_after["name"] == manifest_before["name"]
        assert manifest_after["version"] == manifest_before["version"]

        # Both skill entries present, first one untouched.
        skill_ids = [s["skill_id"] for s in manifest_after["skills"]]
        assert skill_ids == ["first-skill", "second-skill"]

        # The first skill's YAML file was never rewritten.
        assert (pack_dir / "first-skill.yaml").is_file()
        assert (pack_dir / "second-skill.yaml").is_file()

    def test_third_skill_of_different_class_also_appends(self, tmp_path: Path) -> None:
        pack_dir = tmp_path / "demo-pack"
        nsp.new_skill_pack(pack_dir, skill_id="d1", skill_class="domain")
        nsp.new_skill_pack(pack_dir, skill_id="alert", skill_class="system")
        nsp.new_skill_pack(pack_dir, skill_id="flow", skill_class="workflow")

        manifest = yaml.safe_load((pack_dir / "pack.yaml").read_text(encoding="utf-8"))
        skill_ids = {s["skill_id"] for s in manifest["skills"]}
        assert skill_ids == {"d1", "sys-alert", "flow"}
        assert len(manifest["skills"]) == 3


# ---------------------------------------------------------------------------
# Refusal — existing skill YAML must never be clobbered
# ---------------------------------------------------------------------------


class TestRefusesOverwrite:
    def test_refuses_when_skill_yaml_exists(self, tmp_path: Path) -> None:
        pack_dir = tmp_path / "demo-pack"
        nsp.new_skill_pack(pack_dir, skill_id="demo-skill", skill_class="domain")

        skill_path = pack_dir / "demo-skill.yaml"
        original_content = skill_path.read_text(encoding="utf-8")
        original_manifest = (pack_dir / "pack.yaml").read_text(encoding="utf-8")

        result = nsp.new_skill_pack(
            pack_dir, skill_id="demo-skill", skill_class="domain", canonical_name="Different"
        )

        assert result["action"] == "skill_already_exists"
        assert "already exists" in result["error"]
        # Neither file was touched.
        assert skill_path.read_text(encoding="utf-8") == original_content
        assert (pack_dir / "pack.yaml").read_text(encoding="utf-8") == original_manifest

    def test_run_exit_code_1_on_refusal(self, tmp_path: Path) -> None:
        pack_dir = tmp_path / "demo-pack"
        nsp.new_skill_pack(pack_dir, skill_id="demo-skill", skill_class="domain")

        args = argparse.Namespace(
            pack_dir=str(pack_dir),
            skill_id="demo-skill",
            skill_class="domain",
            canonical_name=None,
            pack_name=None,
            json=True,
        )
        exit_code = nsp._run(args)
        assert exit_code == 1

    def test_no_files_written_when_pack_dir_did_not_exist_and_refused(self, tmp_path: Path) -> None:
        """Refusal on an invalid skill_id must not create pack_dir at all."""
        pack_dir = tmp_path / "brand-new"
        result = nsp.new_skill_pack(pack_dir, skill_id="../evil", skill_class="domain")
        assert result["action"] == "invalid_skill_id"
        assert not pack_dir.exists()


# ---------------------------------------------------------------------------
# Domain-skill scaffold: the critical correctness gate
# ---------------------------------------------------------------------------


class TestDomainScaffoldPassesStrictValidation:
    def test_generated_domain_skill_is_lint_clean_under_strict(self, tmp_path: Path) -> None:
        pack_dir = tmp_path / "demo-pack"
        nsp.new_skill_pack(pack_dir, skill_id="demo-skill", skill_class="domain")

        manifest = yaml.safe_load((pack_dir / "pack.yaml").read_text(encoding="utf-8"))
        result = validate_pack_skills(pack_dir, manifest["skills"], strict=True)

        assert result.ok is True, result.format_errors()
        assert result.errors == []

    def test_generated_domain_skill_has_three_fragments_matching_raw_prose(
        self, tmp_path: Path
    ) -> None:
        pack_dir = tmp_path / "demo-pack"
        nsp.new_skill_pack(pack_dir, skill_id="demo-skill", skill_class="domain")
        doc: dict[str, Any] = yaml.safe_load(
            (pack_dir / "demo-skill.yaml").read_text(encoding="utf-8")
        )

        fragments = doc["fragments"]
        assert len(fragments) == 3
        assert [f["fragment_type"] for f in fragments] == [
            "execution",
            "verification",
            "rationale",
        ]
        assert [f["sequence"] for f in fragments] == [0, 1, 2]

        # raw_prose must be the literal concatenation of fragment contents
        # (in order, blank-line separated) — ingest._lint's drift check.
        expected_raw_prose = "\n\n".join(f["content"] for f in fragments)
        assert doc["raw_prose"] == expected_raw_prose

    def test_different_skill_ids_get_lint_clean_scaffolds(self, tmp_path: Path) -> None:
        """Regression guard: tag derivation must stay lint-clean across a range
        of skill_id shapes, including a single-token id."""
        for skill_id in ("single", "multi-word-skill-id", "with_underscore", "a"):
            pack_dir = tmp_path / f"pack-{skill_id}"
            nsp.new_skill_pack(pack_dir, skill_id=skill_id, skill_class="domain")
            manifest = yaml.safe_load((pack_dir / "pack.yaml").read_text(encoding="utf-8"))
            result = validate_pack_skills(pack_dir, manifest["skills"], strict=True)
            assert result.ok is True, f"{skill_id}: {result.format_errors()}"


# ---------------------------------------------------------------------------
# System / workflow scaffolds — at least schema-valid
# ---------------------------------------------------------------------------


class TestSystemScaffold:
    def test_skill_id_auto_prefixed_with_sys(self, tmp_path: Path) -> None:
        pack_dir = tmp_path / "demo-pack"
        result = nsp.new_skill_pack(pack_dir, skill_id="demo-alert", skill_class="system")
        assert result["skill_id"] == "sys-demo-alert"
        assert result["note"] is not None
        assert "sys-demo-alert" in result["note"]
        assert (pack_dir / "sys-demo-alert.yaml").is_file()

    def test_skill_id_already_prefixed_not_double_prefixed(self, tmp_path: Path) -> None:
        pack_dir = tmp_path / "demo-pack"
        result = nsp.new_skill_pack(pack_dir, skill_id="sys-already", skill_class="system")
        assert result["skill_id"] == "sys-already"
        assert result["note"] is None

    def test_generated_system_skill_is_schema_valid(self, tmp_path: Path) -> None:
        pack_dir = tmp_path / "demo-pack"
        nsp.new_skill_pack(pack_dir, skill_id="demo-alert", skill_class="system")
        manifest = yaml.safe_load((pack_dir / "pack.yaml").read_text(encoding="utf-8"))

        # At least schema-valid (no _validate hard errors); a lint warning
        # under --strict is acceptable for a bare-minimum system scaffold.
        result = validate_pack_skills(pack_dir, manifest["skills"], strict=False)
        assert result.ok is True, result.format_errors()

    def test_generated_system_skill_has_no_fragments(self, tmp_path: Path) -> None:
        pack_dir = tmp_path / "demo-pack"
        nsp.new_skill_pack(pack_dir, skill_id="demo-alert", skill_class="system")
        doc = yaml.safe_load((pack_dir / "sys-demo-alert.yaml").read_text(encoding="utf-8"))
        assert "fragments" not in doc or not doc["fragments"]
        assert doc["always_apply"] is True
        assert len(doc["raw_prose"]) >= 80


class TestWorkflowScaffold:
    def test_generated_workflow_skill_is_schema_valid(self, tmp_path: Path) -> None:
        pack_dir = tmp_path / "demo-pack"
        nsp.new_skill_pack(pack_dir, skill_id="demo-flow", skill_class="workflow")
        manifest = yaml.safe_load((pack_dir / "pack.yaml").read_text(encoding="utf-8"))

        result = validate_pack_skills(pack_dir, manifest["skills"], strict=False)
        assert result.ok is True, result.format_errors()

    def test_generated_workflow_skill_has_no_fragments_and_valid_exit_gates(
        self, tmp_path: Path
    ) -> None:
        pack_dir = tmp_path / "demo-pack"
        nsp.new_skill_pack(pack_dir, skill_id="demo-flow", skill_class="workflow")
        doc = yaml.safe_load((pack_dir / "demo-flow.yaml").read_text(encoding="utf-8"))
        assert "fragments" not in doc or not doc["fragments"]
        assert doc["applies_to_phases"] == ["build"]
        assert isinstance(doc["exit_gates"], dict)

        # exit_gates must reference a known predicate structurally.
        assert _validate_gate_spec(doc["exit_gates"]) == []


# ---------------------------------------------------------------------------
# Invalid input handling
# ---------------------------------------------------------------------------


class TestInvalidInput:
    def test_invalid_skill_id_rejected(self, tmp_path: Path) -> None:
        pack_dir = tmp_path / "demo-pack"
        result = nsp.new_skill_pack(pack_dir, skill_id="../evil", skill_class="domain")
        assert result["action"] == "invalid_skill_id"
        assert not (pack_dir / "../evil.yaml").exists()

    def test_invalid_skill_class_rejected(self, tmp_path: Path) -> None:
        pack_dir = tmp_path / "demo-pack"
        result = nsp.new_skill_pack(pack_dir, skill_id="foo", skill_class="not-a-class")
        assert result["action"] == "invalid_skill_class"
        assert not pack_dir.exists()

    def test_pack_dir_is_an_existing_file_refused_cleanly(self, tmp_path: Path) -> None:
        """pack_dir pointing at an existing non-directory (e.g. a typo'd path)
        must refuse cleanly, not crash with an unhandled FileExistsError from
        the later ``pack_dir.mkdir(parents=True, exist_ok=True)``."""
        pack_dir = tmp_path / "not-a-dir"
        pack_dir.write_text("i am a file, not a pack directory", encoding="utf-8")

        result = nsp.new_skill_pack(pack_dir, skill_id="foo", skill_class="domain")

        assert result["action"] == "pack_dir_not_a_directory"
        assert "error" in result
        assert pack_dir.is_file()  # untouched, still a plain file
        assert not (pack_dir.parent / "foo.yaml").exists()


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


class TestCliWiring:
    @staticmethod
    def _parser() -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(prog="agentalloy")
        sub = parser.add_subparsers()
        nsp.add_parser(sub)
        return parser

    def test_parses_required_skill_id(self) -> None:
        args = self._parser().parse_args(["new-skill-pack", "some-dir", "--skill-id", "foo"])
        assert args.pack_dir == "some-dir"
        assert args.skill_id == "foo"
        assert args.skill_class == "domain"
        assert args.func is nsp._run

    def test_skill_class_choices_enforced(self) -> None:
        with pytest.raises(SystemExit):
            self._parser().parse_args(
                ["new-skill-pack", "d", "--skill-id", "foo", "--skill-class", "bogus"]
            )

    def test_run_returns_0_on_success_and_writes_json(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pack_dir = tmp_path / "demo-pack"
        args = argparse.Namespace(
            pack_dir=str(pack_dir),
            skill_id="demo-skill",
            skill_class="domain",
            canonical_name=None,
            pack_name=None,
            json=True,
        )
        exit_code = nsp._run(args)
        assert exit_code == 0
        out = json.loads(capsys.readouterr().out)
        assert out["action"] == "scaffolded"
