# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
"""Unit tests for the Ingestion v2 gates.

Covers:
  - Schema + vocabulary gate (validate_pack_skills)
  - Version gate (check_version_gate)
  - Integration: both gates wired into install_local_pack

Fixture style follows tests/install/test_install_local_pack.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import yaml

from agentalloy.pack_validation import (
    check_version_gate,
    content_hash,
    validate_pack_skills,
)

# ---------------------------------------------------------------------------
# Fixture helpers (mirror test_install_local_pack.py style)
# ---------------------------------------------------------------------------


def _write_skill_yaml(
    pack_dir: Path,
    skill_id: str,
    *,
    fragments: int = 2,
    canonical_name: str | None = None,
    category: str = "engineering",
    extra: dict[str, Any] | None = None,
) -> Path:
    """Write a minimal valid domain-skill YAML."""
    frag_list = [
        {
            "sequence": i + 1,
            "fragment_type": "execution" if i == 0 else "rationale",
            "content": f"This is fragment {i + 1} with enough words to pass the hard floor check here.",
        }
        for i in range(fragments)
    ]
    doc: dict[str, Any] = {
        "skill_type": "domain",
        "skill_id": skill_id,
        "canonical_name": canonical_name or skill_id.replace("-", " ").title(),
        "category": category,
        "skill_class": "domain",
        "domain_tags": ["test"],
        "always_apply": False,
        "phase_scope": ["build"],
        "category_scope": None,
        "author": "test",
        "change_summary": "initial authoring",
        "raw_prose": f"# {skill_id}\n\nThis skill describes {skill_id} in detail. "
        "It provides actionable guidance for engineers working with this domain.",
        "fragments": frag_list,
    }
    if extra:
        doc.update(extra)
    path = pack_dir / f"{skill_id}.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


def _write_pack_manifest(
    pack_dir: Path,
    name: str,
    skills: list[dict[str, Any]],
    *,
    version: str = "1.0.0",
    embed_model: str = "qwen3-embedding:0.6b",
    embedding_dim: int = 1024,
) -> Path:
    manifest: dict[str, Any] = {
        "name": name,
        "version": version,
        "tier": "tooling",
        "description": f"{name} test pack",
        "author": "test",
        "embed_model": embed_model,
        "embedding_dim": embedding_dim,
        "license": "MIT",
        "homepage": "https://example.com",
        "skills": skills,
    }
    path = pack_dir / "pack.yaml"
    path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Gate 1: Schema + vocabulary gate
# ---------------------------------------------------------------------------


class TestSchemaGate:
    def test_valid_skill_passes(self, tmp_path: Path) -> None:
        _write_skill_yaml(tmp_path, "my-skill")
        entries = [{"skill_id": "my-skill", "file": "my-skill.yaml"}]
        result = validate_pack_skills(tmp_path, entries)
        assert result.ok
        assert result.errors == []

    def test_missing_skill_id_fails(self, tmp_path: Path) -> None:
        """A skill YAML with no skill_id is rejected with an actionable message."""
        doc = {
            "skill_type": "domain",
            "skill_id": "",
            "canonical_name": "Missing ID Skill",
            "category": "engineering",
            "skill_class": "domain",
            "domain_tags": [],
            "always_apply": False,
            "phase_scope": ["build"],
            "raw_prose": "Some prose",
            "fragments": [
                {
                    "sequence": 1,
                    "fragment_type": "execution",
                    "content": "Execute something useful with this skill right here.",
                }
            ],
        }
        p = tmp_path / "bad.yaml"
        p.write_text(yaml.safe_dump(doc), encoding="utf-8")
        entries = [{"skill_id": "", "file": "bad.yaml"}]
        result = validate_pack_skills(tmp_path, entries)
        assert not result.ok
        assert any("skill_id" in e for err in result.errors for e in err.errors)

    def test_invalid_category_fails(self, tmp_path: Path) -> None:
        """An unrecognised category for a domain skill is rejected."""
        _write_skill_yaml(tmp_path, "bad-cat", category="nonexistent_category")
        entries = [{"skill_id": "bad-cat", "file": "bad-cat.yaml"}]
        result = validate_pack_skills(tmp_path, entries)
        assert not result.ok
        error_text = result.format_errors()
        assert "nonexistent_category" in error_text

    def test_invalid_fragment_type_fails(self, tmp_path: Path) -> None:
        """A fragment with an invalid fragment_type is rejected."""
        _write_skill_yaml(
            tmp_path,
            "bad-frag",
            extra={
                "fragments": [
                    {
                        "sequence": 1,
                        "fragment_type": "not_a_real_type",
                        "content": "Some content that is long enough for the word count floor check.",
                    }
                ]
            },
        )
        entries = [{"skill_id": "bad-frag", "file": "bad-frag.yaml"}]
        result = validate_pack_skills(tmp_path, entries)
        assert not result.ok
        error_text = result.format_errors()
        assert "not_a_real_type" in error_text

    def test_missing_execution_fragment_fails(self, tmp_path: Path) -> None:
        """A domain skill without an 'execution' fragment fails validation."""
        doc: dict[str, Any] = {
            "skill_type": "domain",
            "skill_id": "no-exec",
            "canonical_name": "No Exec Skill",
            "category": "engineering",
            "skill_class": "domain",
            "domain_tags": ["test"],
            "always_apply": False,
            "phase_scope": ["build"],
            "raw_prose": "Some prose about this skill.",
            "fragments": [
                {
                    "sequence": 1,
                    "fragment_type": "rationale",
                    "content": "Here is the rationale content which is long enough to pass checks.",
                }
            ],
        }
        p = tmp_path / "no-exec.yaml"
        p.write_text(yaml.safe_dump(doc), encoding="utf-8")
        entries = [{"skill_id": "no-exec", "file": "no-exec.yaml"}]
        result = validate_pack_skills(tmp_path, entries)
        assert not result.ok
        error_text = result.format_errors()
        assert "execution" in error_text

    def test_multiple_skills_all_errors_reported(self, tmp_path: Path) -> None:
        """All per-skill errors are collected, not just the first one."""
        _write_skill_yaml(tmp_path, "bad-cat-1", category="INVALID_A")
        _write_skill_yaml(tmp_path, "bad-cat-2", category="INVALID_B")
        entries = [
            {"skill_id": "bad-cat-1", "file": "bad-cat-1.yaml"},
            {"skill_id": "bad-cat-2", "file": "bad-cat-2.yaml"},
        ]
        result = validate_pack_skills(tmp_path, entries)
        assert not result.ok
        assert len(result.errors) == 2

    def test_missing_file_is_skipped_gracefully(self, tmp_path: Path) -> None:
        """A missing file is skipped (caught earlier by _read_pack_manifest)."""
        entries = [{"skill_id": "ghost", "file": "ghost.yaml"}]
        result = validate_pack_skills(tmp_path, entries)
        assert result.ok  # nothing to validate → not an error here

    def test_format_errors_contains_skill_id_and_file(self, tmp_path: Path) -> None:
        """format_errors() includes skill_id and file name for traceability."""
        _write_skill_yaml(tmp_path, "traceable", category="BAD_CAT")
        entries = [{"skill_id": "traceable", "file": "traceable.yaml"}]
        result = validate_pack_skills(tmp_path, entries)
        assert not result.ok
        text = result.format_errors()
        assert "traceable" in text
        assert "traceable.yaml" in text


# ---------------------------------------------------------------------------
# Gate 2: Version gate
# ---------------------------------------------------------------------------


class TestVersionGate:
    def test_new_pack_always_ok(self, tmp_path: Path) -> None:
        """A pack with no prior install record is always allowed through."""
        _write_skill_yaml(tmp_path, "new-skill")
        entries = [{"skill_id": "new-skill", "file": "new-skill.yaml"}]
        result = check_version_gate("my-pack", "1.0.0", tmp_path, entries, [])
        assert result.ok
        assert not result.skip

    def test_identical_content_same_version_is_skip(self, tmp_path: Path) -> None:
        """Same content + same version → silent skip (already_installed)."""
        _write_skill_yaml(tmp_path, "sk")
        _write_pack_manifest(tmp_path, "my-pack", [{"skill_id": "sk", "file": "sk.yaml"}])
        entries = [{"skill_id": "sk", "file": "sk.yaml"}]
        h = content_hash(tmp_path, entries)
        installed: list[dict[str, Any]] = [
            {"name": "my-pack", "version": "1.0.0", "content_hash": h}
        ]
        result = check_version_gate("my-pack", "1.0.0", tmp_path, entries, installed)
        assert result.ok
        assert result.skip

    def test_changed_content_bumped_version_is_ok(self, tmp_path: Path) -> None:
        """Different content + different version → legitimate upgrade."""
        _write_skill_yaml(tmp_path, "sk")
        _write_pack_manifest(tmp_path, "my-pack", [{"skill_id": "sk", "file": "sk.yaml"}])
        entries = [{"skill_id": "sk", "file": "sk.yaml"}]
        # Record stale hash (simulates old install)
        installed: list[dict[str, Any]] = [
            {"name": "my-pack", "version": "1.0.0", "content_hash": "stale_hash_abc123"}
        ]
        result = check_version_gate("my-pack", "1.1.0", tmp_path, entries, installed)
        assert result.ok
        assert not result.skip

    def test_changed_content_same_version_fails(self, tmp_path: Path) -> None:
        """Different content + same version → hard error with actionable message."""
        _write_skill_yaml(tmp_path, "sk")
        entries = [{"skill_id": "sk", "file": "sk.yaml"}]
        # Stale hash — content was changed but version was not bumped.
        installed: list[dict[str, Any]] = [
            {"name": "my-pack", "version": "1.0.0", "content_hash": "stale_hash_abc123"}
        ]
        result = check_version_gate("my-pack", "1.0.0", tmp_path, entries, installed)
        assert not result.ok
        assert not result.skip
        # Error message must explain the version-bump rule.
        assert "version" in result.error.lower()
        assert "1.0.0" in result.error
        assert "bump" in result.error.lower() or "differs" in result.error.lower()

    def test_legacy_state_without_content_hash_is_ok(self, tmp_path: Path) -> None:
        """Installs recorded before the gate existed carry no content_hash.

        Change-vs-unchanged is undecidable there, so the gate must let the
        pack through (same-version re-runs stay a no-op via per-skill ingest
        dedupe) rather than hard-failing every pre-gate install — including
        the `doctor --repair` path, which re-runs install-packs.
        """
        _write_skill_yaml(tmp_path, "sk")
        _write_pack_manifest(tmp_path, "my-pack", [{"skill_id": "sk", "file": "sk.yaml"}])
        entries = [{"skill_id": "sk", "file": "sk.yaml"}]
        installed: list[dict[str, Any]] = [{"name": "my-pack", "version": "1.0.0"}]
        result = check_version_gate("my-pack", "1.0.0", tmp_path, entries, installed)
        assert result.ok
        assert not result.skip

    def test_different_pack_name_not_affected(self, tmp_path: Path) -> None:
        """Only the matching pack name is compared; other packs are ignored."""
        _write_skill_yaml(tmp_path, "sk")
        entries = [{"skill_id": "sk", "file": "sk.yaml"}]
        # Installed state has a DIFFERENT pack name with a stale hash.
        installed: list[dict[str, Any]] = [
            {"name": "other-pack", "version": "1.0.0", "content_hash": "stale_hash"}
        ]
        result = check_version_gate("my-pack", "1.0.0", tmp_path, entries, installed)
        assert result.ok
        assert not result.skip


# ---------------------------------------------------------------------------
# Integration: gates wired into install_local_pack
# ---------------------------------------------------------------------------


class TestInstallLocalPackGatesIntegration:
    """Verify the new gates fire from install_local_pack with the right actions."""

    def test_schema_invalid_action_on_bad_skill(self, tmp_path: Path) -> None:
        """install_local_pack returns action='schema_invalid' for a bad skill YAML."""
        from agentalloy.install.subcommands import install_pack as ip

        _write_skill_yaml(tmp_path, "bad", category="INVALID_CATEGORY_XYZ")
        _write_pack_manifest(
            tmp_path,
            "test-pack",
            [{"skill_id": "bad", "file": "bad.yaml", "fragment_count": 2}],
        )

        with patch.object(ip, "_check_embedding_dim", return_value=None):
            result = ip.install_local_pack(tmp_path, root=tmp_path)

        assert result["action"] == "schema_invalid"
        assert "errors" in result
        errors = result["errors"]
        assert isinstance(errors, list)
        assert len(errors) > 0
        # Must include skill_id and file for traceability.
        assert errors[0]["skill_id"] == "bad"
        assert "bad.yaml" in errors[0]["file"]

    def test_version_unchanged_action_on_stale_content(self, tmp_path: Path) -> None:
        """install_local_pack returns action='version_unchanged' when content changed but version didn't."""
        from agentalloy.install.subcommands import install_pack as ip

        _write_skill_yaml(tmp_path, "good")
        _write_pack_manifest(
            tmp_path,
            "test-pack",
            [{"skill_id": "good", "file": "good.yaml", "fragment_count": 2}],
        )

        # State says pack was installed with a different content hash (old content)
        installed_state = {
            "installed_packs": [
                {
                    "name": "test-pack",
                    "version": "1.0.0",
                    "content_hash": "old_hash_that_is_definitely_wrong",
                }
            ]
        }

        with (
            patch.object(ip, "_check_embedding_dim", return_value=None),
            patch.object(ip.install_state, "load_state", return_value=installed_state),
        ):
            result = ip.install_local_pack(tmp_path, root=tmp_path)

        assert result["action"] == "version_unchanged"
        assert "error" in result
        assert "1.0.0" in result["error"]

    def test_already_installed_via_version_gate_skip(self, tmp_path: Path) -> None:
        """install_local_pack returns action='already_installed' when content is identical."""
        from agentalloy.install.subcommands import install_pack as ip

        _write_skill_yaml(tmp_path, "good")
        _write_pack_manifest(
            tmp_path,
            "test-pack",
            [{"skill_id": "good", "file": "good.yaml", "fragment_count": 2}],
        )
        entries = [{"skill_id": "good", "file": "good.yaml"}]
        current_hash = content_hash(tmp_path, entries)

        installed_state = {
            "installed_packs": [
                {
                    "name": "test-pack",
                    "version": "1.0.0",
                    "content_hash": current_hash,
                }
            ]
        }

        with (
            patch.object(ip, "_check_embedding_dim", return_value=None),
            patch.object(ip.install_state, "load_state", return_value=installed_state),
        ):
            result = ip.install_local_pack(tmp_path, root=tmp_path)

        assert result["action"] == "already_installed"

    def test_good_new_pack_passes_all_gates(self, tmp_path: Path) -> None:
        """A valid new pack with no prior install passes both gates and reaches ingest."""
        from agentalloy.install.subcommands import install_pack as ip

        _write_skill_yaml(tmp_path, "good")
        _write_pack_manifest(
            tmp_path,
            "new-pack",
            [{"skill_id": "good", "file": "good.yaml", "fragment_count": 2}],
        )

        # Simulate all skills already-present (duplicate) from ingest so we
        # don't need a real DB.
        fake_ingest = {
            "yaml": "good.yaml",
            "exit_code": 4,
            "outcome": "duplicate",
            "stdout_tail": "",
            "stderr_tail": "",
        }

        corpus_dir = tmp_path / "corpus"
        corpus_dir.mkdir()
        (corpus_dir / "skills.duck").touch()
        (corpus_dir / "ladybug").mkdir()

        with (
            patch.object(ip, "_check_embedding_dim", return_value=None),
            patch.object(ip, "_ingest_yaml", return_value=fake_ingest),
            patch.object(ip.install_state, "load_state", return_value={}),
            patch.object(ip.install_state, "save_state"),
            patch.object(ip.install_state, "record_step"),
            patch.object(ip.install_state, "corpus_dir", return_value=corpus_dir),
            patch(
                "agentalloy.config.get_settings",
                return_value=MagicMock(
                    duckdb_path=str(corpus_dir / "skills.duck"),
                    ladybug_db_path=str(corpus_dir / "ladybug"),
                ),
            ),
        ):
            result = ip.install_local_pack(tmp_path, root=tmp_path)

        # Should not fail at schema or version gates; outcome should be
        # already_installed (all duplicates) or ingested.
        assert result["action"] in ("already_installed", "ingested")
