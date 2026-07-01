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
from unittest.mock import ANY, MagicMock, patch

import yaml

from agentalloy.pack_validation import (
    check_version_gate,
    content_hash,
    validate_pack_skills,
)

# ---------------------------------------------------------------------------
# Fixture helpers (mirror test_install_local_pack.py style)
# ---------------------------------------------------------------------------


# Fragment content templates that are simultaneously:
#   - >= _FRAG_WORDS_WARN_MIN (25) words, so `_lint` doesn't flag them as
#     under-discriminative for nomic-embed-text-v1.5 (folded into hard errors
#     under --strict);
#   - reused verbatim in `raw_prose` below, so `_lint`'s content-drift check
#     ("fragment content is not a contiguous slice of raw_prose") never fires.
_LINT_CLEAN_FRAGMENT_TEMPLATES: dict[str, str] = {
    "execution": (
        "Run the {skill_id} workflow end to end by gathering every required input "
        "value, invoking the primary command with those inputs, waiting for it to "
        "finish, and confirming the operation completed without raising any errors "
        "before moving on to the next stage of the task."
    ),
    "verification": (
        "After completing the {skill_id} steps, verify the outcome by checking that "
        "the expected artifacts exist on disk, the logs show no unexpected errors, "
        "and any downstream consumer can read the produced output without further "
        "manual intervention."
    ),
    "rationale": (
        "This approach is recommended for {skill_id} because it keeps the workflow "
        "predictable and auditable, reduces the chance of a silent failure going "
        "unnoticed, and matches the conventions already established elsewhere in "
        "the corpus for comparable domain skills."
    ),
    "example": (
        "For example, a typical {skill_id} invocation supplies a small, realistic "
        "input, runs the command exactly as documented, and inspects the resulting "
        "output to confirm it matches the documented shape before trusting it in a "
        "larger automated pipeline."
    ),
}

# execution first (hard-required by `_validate`), then verification and
# rationale (both required by `_lint` under --strict) — anything beyond
# index 2 cycles back through `example` so larger fixtures stay lint-clean.
_LINT_CLEAN_TYPE_ORDER = ["execution", "verification", "rationale", "example"]


def _write_skill_yaml(
    pack_dir: Path,
    skill_id: str,
    *,
    fragments: int = 3,
    canonical_name: str | None = None,
    category: str = "engineering",
    extra: dict[str, Any] | None = None,
) -> Path:
    """Write a lint-clean domain-skill YAML (passes `ingest._lint` under --strict).

    Lint-clean requires >= 3 fragments: `execution` is hard-required by
    `_validate`, and `_lint` (under --strict, the new install-pack default)
    additionally requires a `rationale` and a `verification` fragment — with
    only 1-2 fragments, at least one of those is structurally impossible to
    include, so `fragments < 3` will not pass a strict Gate 1. Callers that
    never reach a strict lint gate (e.g. `_read_pack_manifest` drift checks,
    `check_version_gate` unit tests) may still use `fragments < 3`.
    """
    frag_types = [_LINT_CLEAN_TYPE_ORDER[i % len(_LINT_CLEAN_TYPE_ORDER)] for i in range(fragments)]
    frag_contents = [
        _LINT_CLEAN_FRAGMENT_TEMPLATES[t].format(skill_id=skill_id) for t in frag_types
    ]
    frag_list = [
        {"sequence": i + 1, "fragment_type": t, "content": c}
        for i, (t, c) in enumerate(zip(frag_types, frag_contents, strict=True))
    ]
    raw_prose = f"# {skill_id}\n\n" + "\n\n".join(frag_contents)
    doc: dict[str, Any] = {
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
        "raw_prose": raw_prose,
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
    embed_model: str = "nomic-embed-text-v1.5.Q8_0.gguf",
    embedding_dim: int = 768,
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
        # Default strict=True: the fixture is lint-clean, so this exercises
        # both _validate and _lint together.
        result = validate_pack_skills(tmp_path, entries)
        assert result.ok
        assert result.errors == []

    def test_missing_skill_id_fails(self, tmp_path: Path) -> None:
        """A skill YAML with no skill_id is rejected with an actionable message."""
        doc = {
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
        # Fresh install is not a content change — must not force re-ingest.
        assert not result.changed

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
        # A no-op skip is not a content change — must not force re-ingest.
        assert not result.changed

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
        # Changed-under-bump must force re-ingest: the skill already exists in the
        # graph, so a plain re-ingest would skip it as a duplicate and the corpus
        # would keep serving stale prose.
        assert result.changed

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
        # Undecidable legacy state is not a known content change — don't force.
        assert not result.changed

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
            [{"skill_id": "bad", "file": "bad.yaml", "fragment_count": 3}],
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
            [{"skill_id": "good", "file": "good.yaml", "fragment_count": 3}],
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
            # Default strict=True: the fixture is lint-clean, so Gate 1 passes
            # for real and the version gate (not lint) drives this outcome.
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
            [{"skill_id": "good", "file": "good.yaml", "fragment_count": 3}],
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
            # Default strict=True: the fixture is lint-clean, so Gate 1 passes
            # for real and the version gate (not lint) drives this outcome.
            result = ip.install_local_pack(tmp_path, root=tmp_path)

        assert result["action"] == "already_installed"

    def test_good_new_pack_passes_all_gates(self, tmp_path: Path) -> None:
        """A valid new pack with no prior install passes both gates and reaches ingest."""
        from agentalloy.install.subcommands import install_pack as ip

        _write_skill_yaml(tmp_path, "good")
        _write_pack_manifest(
            tmp_path,
            "new-pack",
            [{"skill_id": "good", "file": "good.yaml", "fragment_count": 3}],
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
        (corpus_dir / "agentalloy.duck").touch()
        (corpus_dir / "fragments.lance").mkdir()

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
                    duckdb_path=str(corpus_dir / "agentalloy.duck"),
                    fragments_lance_path=str(corpus_dir / "fragments.lance"),
                ),
            ),
        ):
            # Default strict=True: the fixture is lint-clean, so this genuinely
            # exercises Gate 1's --strict lint fold, not just gate plumbing.
            result = ip.install_local_pack(tmp_path, root=tmp_path)

        # Should not fail at schema or version gates; outcome should be
        # already_installed (all duplicates) or ingested.
        assert result["action"] in ("already_installed", "ingested")

    def test_version_bump_forces_reingest_and_invalidates_vectors(self, tmp_path: Path) -> None:
        """A version-bumped pack with changed content force re-ingests its skills
        AND drops their stale vectors, so the corpus stops serving old prose.

        Regression: previously the version gate passed the bump through (ok=True)
        but the per-skill ingest skipped the existing skill_id as a DUPLICATE, so
        neither the graph prose nor the vectors updated — the bump was recorded in
        install state but never reached the corpus.
        """
        from agentalloy.install.subcommands import install_pack as ip

        _write_skill_yaml(tmp_path, "good")
        _write_pack_manifest(
            tmp_path,
            "test-pack",
            [{"skill_id": "good", "file": "good.yaml", "fragment_count": 3}],
            version="1.1.0",
        )

        # Prior install at an OLDER version with a stale hash → gate sees a
        # legitimate, content-changing upgrade (changed=True).
        installed_state = {
            "installed_packs": [
                {
                    "name": "test-pack",
                    "version": "1.0.0",
                    "content_hash": "old_hash_that_is_definitely_wrong",
                }
            ]
        }

        # Force re-ingest overwrites the existing skill → outcome "ingested".
        fake_ingest = {
            "yaml": "good.yaml",
            "exit_code": 0,
            "outcome": "ingested",
            "stdout_tail": "",
            "stderr_tail": "",
        }

        corpus_dir = tmp_path / "corpus"
        corpus_dir.mkdir()
        (corpus_dir / "agentalloy.duck").touch()
        (corpus_dir / "fragments.lance").mkdir()

        ingest_mock = MagicMock(return_value=fake_ingest)
        invalidate_mock = MagicMock(return_value=3)

        with (
            patch.object(ip, "_check_embedding_dim", return_value=None),
            patch.object(ip, "_ingest_yaml", ingest_mock),
            patch.object(ip, "_invalidate_pack_vectors", invalidate_mock),
            patch.object(ip.install_state, "load_state", return_value=installed_state),
            patch.object(ip.install_state, "save_state"),
            patch.object(ip.install_state, "record_step"),
            patch.object(ip.install_state, "corpus_dir", return_value=corpus_dir),
            patch(
                "agentalloy.config.get_settings",
                return_value=MagicMock(
                    duckdb_path=str(corpus_dir / "agentalloy.duck"),
                    fragments_lance_path=str(corpus_dir / "fragments.lance"),
                ),
            ),
            # This run ingests a skill (outcome="ingested"), which triggers the
            # post-ingest reembed/dedup wiring — stub it so the test doesn't hit
            # a real embed server/DB.
            patch("agentalloy.reembed.cli.run_bulk_reembed", return_value=0) as reembed_mock,
        ):
            # Default strict=True: the fixture is lint-clean, so Gate 1 passes
            # for real; this test's own focus (force re-ingest + vector
            # invalidation) is exercised on top of that.
            result = ip.install_local_pack(tmp_path, root=tmp_path)

        reembed_mock.assert_called_once()
        assert result["action"] == "ingested"
        assert result["dedup_exit_code"] == 0
        # Every skill must be ingested with force=True so the existing node is
        # overwritten rather than skipped as a duplicate.
        assert ingest_mock.call_count == 1
        assert ingest_mock.call_args.kwargs["force"] is True
        # The pack's stale vectors must be invalidated so the downstream reembed
        # re-creates them from the new prose.
        invalidate_mock.assert_called_once()
        passed_entries = invalidate_mock.call_args.args[0]
        assert any(e.get("skill_id") == "good" for e in passed_entries)

    def test_missing_rationale_fragment_rejected_under_strict(self, tmp_path: Path) -> None:
        """A skill missing a 'rationale' fragment fails Gate 1 under --strict
        (the new install_local_pack default): the lint message must appear in
        errors, and no ingest subprocess may be spawned.
        """
        from agentalloy.install.subcommands import install_pack as ip

        exec_content = _LINT_CLEAN_FRAGMENT_TEMPLATES["execution"].format(skill_id="no-rationale")
        verify_content = _LINT_CLEAN_FRAGMENT_TEMPLATES["verification"].format(
            skill_id="no-rationale"
        )
        _write_skill_yaml(
            tmp_path,
            "no-rationale",
            extra={
                "fragments": [
                    {"sequence": 1, "fragment_type": "execution", "content": exec_content},
                    {"sequence": 2, "fragment_type": "verification", "content": verify_content},
                ],
                # Otherwise fully lint-clean (execution + verification present,
                # content matches raw_prose, plenty of words) so the ONLY lint
                # signal is the missing 'rationale' fragment.
                "raw_prose": f"# no-rationale\n\n{exec_content}\n\n{verify_content}",
            },
        )
        _write_pack_manifest(
            tmp_path,
            "test-pack",
            [{"skill_id": "no-rationale", "file": "no-rationale.yaml", "fragment_count": 2}],
        )

        with (
            patch.object(ip, "_check_embedding_dim", return_value=None),
            patch.object(ip, "_ingest_yaml") as ingest_mock,
        ):
            result = ip.install_local_pack(tmp_path, root=tmp_path)

        assert result["action"] == "schema_invalid"
        errors = result["errors"]
        assert len(errors) == 1
        assert errors[0]["skill_id"] == "no-rationale"
        assert any("rationale" in e for e in errors[0]["errors"])
        # Gate 1 rejects before any ingest subprocess is spawned.
        ingest_mock.assert_not_called()

    def test_missing_rationale_fragment_allowed_with_strict_false(self, tmp_path: Path) -> None:
        """The same missing-rationale fixture installs cleanly with
        strict=False (the ``--allow-lint-warnings`` CLI flag's equivalent) —
        _validate hard errors still block, but lint warnings no longer do.
        """
        from agentalloy.install.subcommands import install_pack as ip

        exec_content = _LINT_CLEAN_FRAGMENT_TEMPLATES["execution"].format(skill_id="no-rationale")
        verify_content = _LINT_CLEAN_FRAGMENT_TEMPLATES["verification"].format(
            skill_id="no-rationale"
        )
        _write_skill_yaml(
            tmp_path,
            "no-rationale",
            extra={
                "fragments": [
                    {"sequence": 1, "fragment_type": "execution", "content": exec_content},
                    {"sequence": 2, "fragment_type": "verification", "content": verify_content},
                ],
                "raw_prose": f"# no-rationale\n\n{exec_content}\n\n{verify_content}",
            },
        )
        _write_pack_manifest(
            tmp_path,
            "test-pack",
            [{"skill_id": "no-rationale", "file": "no-rationale.yaml", "fragment_count": 2}],
        )

        fake_ingest = {
            "yaml": "no-rationale.yaml",
            "exit_code": 0,
            "outcome": "ingested",
            "stdout_tail": "",
            "stderr_tail": "",
        }
        corpus_dir = tmp_path / "corpus"
        corpus_dir.mkdir()
        (corpus_dir / "skills.duck").touch()
        (corpus_dir / "ladybug").mkdir()

        with (
            patch.object(ip, "_check_embedding_dim", return_value=None),
            patch.object(ip, "_ingest_yaml", return_value=fake_ingest) as ingest_mock,
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
            # new_count > 0 triggers the post-ingest reembed/dedup wiring —
            # stub it so the test doesn't hit a real embed server/DB.
            patch("agentalloy.reembed.cli.run_bulk_reembed", return_value=0) as reembed_mock,
        ):
            result = ip.install_local_pack(tmp_path, root=tmp_path, strict=False)

        assert result["action"] == "ingested"
        assert result.get("errors") is None
        ingest_mock.assert_called_once()
        # strict=False must still reach the ingest subprocess with --strict
        # omitted — see TestIngestYamlStrictFlag below for the direct
        # subprocess-cmd assertion; here we only assert the gate let it through.
        reembed_mock.assert_called_once()


# ---------------------------------------------------------------------------
# _ingest_yaml's strict -> --strict subprocess-cmd translation
# ---------------------------------------------------------------------------


class TestIngestYamlStrictFlag:
    """Direct unit coverage for `_ingest_yaml`'s `strict` -> subprocess `cmd`
    translation (install_pack.py:292-293). Every higher-level test above
    mocks `_ingest_yaml` itself, so nothing else in the suite would catch a
    regression that drops or inverts `if strict: cmd.append("--strict")`.
    """

    @staticmethod
    def _write_yaml(tmp_path: Path) -> Path:
        yaml_path = tmp_path / "skill.yaml"
        yaml_path.write_text("skill_id: some-skill\n", encoding="utf-8")
        return yaml_path

    def test_strict_true_appends_strict_flag(self, tmp_path: Path) -> None:
        from agentalloy.install.subcommands import install_pack as ip

        yaml_path = self._write_yaml(tmp_path)
        fake_result = MagicMock(returncode=0, stdout="", stderr="")
        with patch.object(ip.subprocess, "run", return_value=fake_result) as run_mock:
            ip._ingest_yaml(yaml_path, tmp_path, strict=True)

        cmd = run_mock.call_args.args[0]
        assert "--strict" in cmd

    def test_strict_false_omits_strict_flag(self, tmp_path: Path) -> None:
        from agentalloy.install.subcommands import install_pack as ip

        yaml_path = self._write_yaml(tmp_path)
        fake_result = MagicMock(returncode=0, stdout="", stderr="")
        with patch.object(ip.subprocess, "run", return_value=fake_result) as run_mock:
            ip._ingest_yaml(yaml_path, tmp_path, strict=False)

        cmd = run_mock.call_args.args[0]
        assert "--strict" not in cmd


# ---------------------------------------------------------------------------
# Dedup gate wiring — install_local_pack/install_pack -> run_bulk_reembed
# ---------------------------------------------------------------------------


class TestDedupGateWiring:
    """`install_local_pack`/`install_pack` fold `run_bulk_reembed`'s exit code
    into `dedup_exit_code` and the resulting `remediation`, mirroring
    `install-packs`' existing "WARN: bulk reembed exited non-zero" pattern.

    A full end-to-end dedup test (two packs, near-paraphrase skills, real
    embeddings, hard-match at `dedup_hard_threshold`) needs a live embed
    server — out of scope for this mocked-network/subprocess test file (see
    `tests/test_dedup_gate.py` for the dedup classification logic itself,
    exercised there with deterministic synthetic vectors). This class
    verifies the wiring point instead: `run_bulk_reembed`'s return code
    propagates end to end.
    """

    @staticmethod
    def _good_pack(tmp_path: Path) -> None:
        _write_skill_yaml(tmp_path, "good")
        _write_pack_manifest(
            tmp_path,
            "test-pack",
            [{"skill_id": "good", "file": "good.yaml", "fragment_count": 3}],
        )

    @staticmethod
    def _corpus_dir(tmp_path: Path) -> Path:
        corpus_dir = tmp_path / "corpus"
        corpus_dir.mkdir()
        (corpus_dir / "skills.duck").touch()
        (corpus_dir / "ladybug").mkdir()
        return corpus_dir

    def test_dedup_exit_nonzero_propagates_to_result(self, tmp_path: Path) -> None:
        """A hard cross-pack duplicate (`run_bulk_reembed` -> EXIT_DEDUP)
        surfaces as a non-zero `dedup_exit_code` and a WARN remediation —
        vectors are still written (the gate reports, it doesn't roll back).
        """
        from agentalloy.install.subcommands import install_pack as ip
        from agentalloy.reembed.cli import EXIT_DEDUP

        self._good_pack(tmp_path)
        corpus_dir = self._corpus_dir(tmp_path)
        fake_ingest = {
            "yaml": "good.yaml",
            "exit_code": 0,
            "outcome": "ingested",
            "stdout_tail": "",
            "stderr_tail": "",
        }

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
            patch(
                "agentalloy.reembed.cli.run_bulk_reembed", return_value=EXIT_DEDUP
            ) as reembed_mock,
        ):
            result = ip.install_local_pack(tmp_path, root=tmp_path)

        reembed_mock.assert_called_once()
        assert result["action"] == "ingested"
        assert result["dedup_exit_code"] == EXIT_DEDUP
        assert "WARN" in (result.get("remediation") or "")
        assert "reembed exited non-zero" in (result.get("remediation") or "")

    def test_allow_duplicates_forwarded_to_run_bulk_reembed(self, tmp_path: Path) -> None:
        """``allow_duplicates=True`` (the CLI ``--allow-duplicates`` flag) must
        reach ``run_bulk_reembed`` so a knowingly-accepted cross-pack overlap
        downgrades from a failing exit code to a warning.
        """
        from agentalloy.install.subcommands import install_pack as ip

        self._good_pack(tmp_path)
        corpus_dir = self._corpus_dir(tmp_path)
        fake_ingest = {
            "yaml": "good.yaml",
            "exit_code": 0,
            "outcome": "ingested",
            "stdout_tail": "",
            "stderr_tail": "",
        }

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
            patch("agentalloy.reembed.cli.run_bulk_reembed", return_value=0) as reembed_mock,
        ):
            result = ip.install_local_pack(tmp_path, root=tmp_path, allow_duplicates=True)

        assert result["action"] == "ingested"
        assert result["dedup_exit_code"] == 0
        reembed_mock.assert_called_once_with(
            no_restart=False, allow_duplicates=True, result_sink=ANY
        )

    def test_hard_and_soft_matches_propagate_from_result_sink(self, tmp_path: Path) -> None:
        """`run_bulk_reembed` populates its `result_sink` kwarg with
        `dedup_hard`/`dedup_soft` match detail (skill IDs, fragment IDs,
        similarity) — `install_local_pack` must surface that as
        `dedup_hard_matches`/`dedup_soft_matches` in its own result, not just
        the bare exit code.
        """
        from agentalloy.install.subcommands import install_pack as ip
        from agentalloy.reembed.cli import EXIT_DEDUP

        self._good_pack(tmp_path)
        corpus_dir = self._corpus_dir(tmp_path)
        fake_ingest = {
            "yaml": "good.yaml",
            "exit_code": 0,
            "outcome": "ingested",
            "stdout_tail": "",
            "stderr_tail": "",
        }
        hard_match = {
            "incoming_skill_id": "good",
            "existing_skill_id": "other-skill",
            "fragment_id_incoming": "good-v1-f1",
            "fragment_id_existing": "other-skill-v1-f2",
            "similarity": 0.97,
            "verdict": "hard",
        }

        def fake_run_bulk_reembed(
            no_restart: bool = False,
            allow_duplicates: bool = False,
            *,
            result_sink: dict[str, Any] | None = None,
        ) -> int:
            if result_sink is not None:
                result_sink["dedup_hard"] = [hard_match]
                result_sink["dedup_soft"] = []
            return EXIT_DEDUP

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
            patch("agentalloy.reembed.cli.run_bulk_reembed", side_effect=fake_run_bulk_reembed),
        ):
            result = ip.install_local_pack(tmp_path, root=tmp_path)

        assert result["dedup_exit_code"] == EXIT_DEDUP
        assert result["dedup_hard_matches"] == [hard_match]
        assert result["dedup_soft_matches"] == []

    def test_run_reembed_false_skips_the_call_entirely(self, tmp_path: Path) -> None:
        """``run_reembed=False`` (install-packs' bulk-bootstrap loop) must
        skip this call's own reembed pass even though a skill was ingested —
        install-packs triggers exactly one reembed for the whole run, after
        its loop, not one per pack (see install_packs.py's
        `_run_container_guard`).
        """
        from agentalloy.install.subcommands import install_pack as ip

        self._good_pack(tmp_path)
        corpus_dir = self._corpus_dir(tmp_path)
        fake_ingest = {
            "yaml": "good.yaml",
            "exit_code": 0,
            "outcome": "ingested",
            "stdout_tail": "",
            "stderr_tail": "",
        }

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
            patch("agentalloy.reembed.cli.run_bulk_reembed") as reembed_mock,
        ):
            result = ip.install_local_pack(tmp_path, root=tmp_path, run_reembed=False)

        assert result["action"] == "ingested"
        assert result["dedup_exit_code"] is None
        assert result["dedup_hard_matches"] == []
        reembed_mock.assert_not_called()

    def test_dedup_exit_zero_by_default_when_no_new_skills(self, tmp_path: Path) -> None:
        """`run_bulk_reembed` is never invoked when nothing new was ingested
        (e.g. every skill in the pack was already a duplicate) — matches the
        existing bulk-reembed "only fires on new fragments" guard.
        """
        from agentalloy.install.subcommands import install_pack as ip

        self._good_pack(tmp_path)
        corpus_dir = self._corpus_dir(tmp_path)
        fake_ingest = {
            "yaml": "good.yaml",
            "exit_code": 4,
            "outcome": "duplicate",
            "stdout_tail": "",
            "stderr_tail": "",
        }

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
            patch("agentalloy.reembed.cli.run_bulk_reembed") as reembed_mock,
        ):
            result = ip.install_local_pack(tmp_path, root=tmp_path)

        assert result["action"] == "already_installed"
        assert result["dedup_exit_code"] is None
        reembed_mock.assert_not_called()
