# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
"""Unit tests for the ``validate-pack`` subcommand.

Covers: a valid pack exits 0 with zero ingest/reembed side effects, an
invalid pack (missing rationale fragment under --strict) exits 1 with the
specific error message surfaced, a pack_dir with no pack.yaml exits 2, a
nonexistent pack_dir exits 2, and ``--allow-lint-warnings`` downgrades a
lint-only failure to a pass — mirroring install-pack's identical flag.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from agentalloy.install.subcommands import new_skill_pack as nsp
from agentalloy.install.subcommands import validate_pack as vp

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_pack_manifest(pack_dir: Path, name: str, skills: list[dict[str, Any]]) -> None:
    manifest = {
        "name": name,
        "version": "1.0.0",
        "tier": "domain",
        "description": f"{name} test pack",
        "author": "test",
        "embed_model": "nomic-embed-text-v1.5",
        "embedding_dim": 768,
        "license": "MIT",
        "always_install": False,
        "skills": skills,
    }
    (pack_dir / "pack.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")


def _write_missing_rationale_skill(pack_dir: Path, skill_id: str) -> None:
    """A domain skill YAML with execution + verification but no rationale
    fragment — passes ingest._validate (execution present) but fails
    ingest._lint's 'no rationale fragment' check, which strict=True folds
    into a hard Gate 1 failure."""
    execution = (
        "Run the primary command for this task end to end, gathering every "
        "required input value first, then invoking the command and waiting "
        "for it to finish without raising any unexpected errors along the way."
    )
    verification = (
        "Confirm the task completed correctly by checking that the expected "
        "output artifact exists on disk and that no error lines appear in "
        "the captured logs before reporting success to the caller."
    )
    doc = {
        "skill_id": skill_id,
        "canonical_name": skill_id.replace("-", " ").title(),
        "description": "test fixture",
        "category": "tooling",
        "skill_class": "domain",
        "domain_tags": ["fixture-alpha", "fixture-beta"],
        "always_apply": False,
        "phase_scope": [],
        "category_scope": [],
        "author": "test",
        "change_summary": "test fixture",
        "raw_prose": f"{execution}\n\n{verification}",
        "fragments": [
            {"sequence": 0, "fragment_type": "execution", "content": execution},
            {"sequence": 1, "fragment_type": "verification", "content": verification},
        ],
    }
    (pack_dir / f"{skill_id}.yaml").write_text(
        yaml.safe_dump(doc, sort_keys=False), encoding="utf-8"
    )


def _run_args(
    pack_dir: Path, *, allow_lint_warnings: bool = False, as_json: bool = True
) -> argparse.Namespace:
    return argparse.Namespace(
        pack_dir=str(pack_dir),
        allow_lint_warnings=allow_lint_warnings,
        json=as_json,
    )


# ---------------------------------------------------------------------------
# Valid pack -> exit 0, zero side effects
# ---------------------------------------------------------------------------


class TestValidPackNoSideEffects:
    def test_valid_pack_returns_ok_and_exit_0(self, tmp_path: Path) -> None:
        pack_dir = tmp_path / "demo-pack"
        nsp.new_skill_pack(pack_dir, skill_id="demo-skill", skill_class="domain")

        result = vp.validate_pack(pack_dir, strict=True)
        assert result["action"] == "valid"
        assert result["ok"] is True
        assert result["errors"] == []

        exit_code = vp._run(_run_args(pack_dir))
        assert exit_code == 0

    def test_no_ingest_or_reembed_functions_called(self, tmp_path: Path) -> None:
        """validate-pack must be a pure dry-run: no ingestion, no reembed."""
        pack_dir = tmp_path / "demo-pack"
        nsp.new_skill_pack(pack_dir, skill_id="demo-skill", skill_class="domain")

        with (
            patch(
                "agentalloy.install.subcommands.install_pack.install_local_pack",
                side_effect=AssertionError("install_local_pack must not be called"),
            ) as mock_install,
            patch(
                "agentalloy.ingest._single",
                side_effect=AssertionError("ingest._single must not be called"),
            ) as mock_ingest_single,
            patch(
                "agentalloy.ingest._batch",
                side_effect=AssertionError("ingest._batch must not be called"),
            ) as mock_ingest_batch,
        ):
            result = vp.validate_pack(pack_dir, strict=True)

        assert result["ok"] is True
        mock_install.assert_not_called()
        mock_ingest_single.assert_not_called()
        mock_ingest_batch.assert_not_called()

    def test_no_corpus_directory_created(self, tmp_path: Path) -> None:
        """No LadybugDB/DuckDB files should appear anywhere as a side effect."""
        pack_dir = tmp_path / "demo-pack"
        nsp.new_skill_pack(pack_dir, skill_id="demo-skill", skill_class="domain")

        before = set(tmp_path.rglob("*"))
        vp.validate_pack(pack_dir, strict=True)
        after = set(tmp_path.rglob("*"))

        assert after == before  # no new files/dirs anywhere under tmp_path


# ---------------------------------------------------------------------------
# Invalid pack -> exit 1 with specific error surfaced
# ---------------------------------------------------------------------------


class TestInvalidPack:
    def test_missing_rationale_fragment_fails_strict(self, tmp_path: Path) -> None:
        pack_dir = tmp_path / "demo-pack"
        pack_dir.mkdir()
        _write_missing_rationale_skill(pack_dir, "no-rationale")
        _write_pack_manifest(
            pack_dir,
            "demo-pack",
            [{"skill_id": "no-rationale", "file": "no-rationale.yaml", "fragment_count": 2}],
        )

        result = vp.validate_pack(pack_dir, strict=True)
        assert result["ok"] is False
        assert result["action"] == "invalid"
        assert any("rationale" in msg for msg in result["formatted_errors"].splitlines())

        exit_code = vp._run(_run_args(pack_dir))
        assert exit_code == 1

    def test_error_message_surfaced_in_json_output(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pack_dir = tmp_path / "demo-pack"
        pack_dir.mkdir()
        _write_missing_rationale_skill(pack_dir, "no-rationale")
        _write_pack_manifest(
            pack_dir,
            "demo-pack",
            [{"skill_id": "no-rationale", "file": "no-rationale.yaml", "fragment_count": 2}],
        )

        exit_code = vp._run(_run_args(pack_dir, as_json=True))
        assert exit_code == 1
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is False
        joined = json.dumps(out)
        assert "rationale" in joined

    def test_allow_lint_warnings_downgrades_lint_only_failure_to_pass(self, tmp_path: Path) -> None:
        """A lint-only issue (missing rationale) is a hard failure under the
        default strict=True, but passes with --allow-lint-warnings (mirrors
        install-pack's identical flag semantics)."""
        pack_dir = tmp_path / "demo-pack"
        pack_dir.mkdir()
        _write_missing_rationale_skill(pack_dir, "no-rationale")
        _write_pack_manifest(
            pack_dir,
            "demo-pack",
            [{"skill_id": "no-rationale", "file": "no-rationale.yaml", "fragment_count": 2}],
        )

        strict_result = vp.validate_pack(pack_dir, strict=True)
        assert strict_result["ok"] is False

        lenient_result = vp.validate_pack(pack_dir, strict=False)
        assert lenient_result["ok"] is True

        exit_code = vp._run(_run_args(pack_dir, allow_lint_warnings=True))
        assert exit_code == 0


# ---------------------------------------------------------------------------
# Usage errors -> exit 2
# ---------------------------------------------------------------------------


class TestUsageErrors:
    def test_no_pack_yaml_exit_2(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        pack_dir = tmp_path / "no-manifest"
        pack_dir.mkdir()

        result = vp.validate_pack(pack_dir)
        assert result["action"] == "usage_error"

        exit_code = vp._run(_run_args(pack_dir))
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "pack.yaml" in err

    def test_nonexistent_dir_exit_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pack_dir = tmp_path / "does-not-exist"

        result = vp.validate_pack(pack_dir)
        assert result["action"] == "usage_error"

        exit_code = vp._run(_run_args(pack_dir))
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "not a directory" in err

    def test_file_instead_of_dir_exit_2(self, tmp_path: Path) -> None:
        not_a_dir = tmp_path / "some-file.txt"
        not_a_dir.write_text("not a pack")

        result = vp.validate_pack(not_a_dir)
        assert result["action"] == "usage_error"


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


class TestCliWiring:
    @staticmethod
    def _parser() -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(prog="agentalloy")
        sub = parser.add_subparsers()
        vp.add_parser(sub)
        return parser

    def test_parses_pack_dir_positional(self) -> None:
        args = self._parser().parse_args(["validate-pack", "some-dir"])
        assert args.pack_dir == "some-dir"
        assert args.allow_lint_warnings is False
        assert args.func is vp._run

    def test_allow_lint_warnings_flag_parses(self) -> None:
        args = self._parser().parse_args(["validate-pack", "d", "--allow-lint-warnings"])
        assert args.allow_lint_warnings is True

    def test_run_via_parsed_args_end_to_end(self, tmp_path: Path) -> None:
        pack_dir = tmp_path / "demo-pack"
        nsp.new_skill_pack(pack_dir, skill_id="demo-skill", skill_class="domain")

        args = self._parser().parse_args(["validate-pack", str(pack_dir), "--json"])
        exit_code = args.func(args)
        assert exit_code == 0
