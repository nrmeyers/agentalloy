"""Tests for the ``artifact`` CLI — deliverables go straight to the store.

The verb exists so workflow artifacts (spec, approach, tasks, test plan) are
recorded into the state store without ever touching disk: content arrives on
stdin (or from an already-existing file) and the store is the artifact's only
home.
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

import pytest

from agentalloy.install.subcommands import artifact as artifact_mod
from agentalloy.install.subcommands.artifact import run_artifact_put


class TestArtifactSubcommandParsing:
    @staticmethod
    def _parser() -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(prog="agentalloy")
        sub = parser.add_subparsers()
        artifact_mod.add_parser(sub)
        return parser

    def test_put_parses_with_required_identifiers(self) -> None:
        args = self._parser().parse_args(
            ["artifact", "put", "--phase", "plan", "--slug", "llm-config", "--name", "tasks.artifact"]
        )
        assert args.func is run_artifact_put
        assert args.phase == "plan"
        assert args.slug == "llm-config"
        assert args.name == "tasks.artifact"
        assert args.file is None

    def test_missing_identifier_rejected(self) -> None:
        with pytest.raises(SystemExit):
            self._parser().parse_args(["artifact", "put", "--phase", "plan", "--slug", "x"])


@pytest.fixture()
def repo_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "pyproject.toml").write_text("")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _stdin(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))


class TestArtifactPut:
    def test_reads_stdin_and_records(self, repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _stdin(monkeypatch, "# Tasks\n\nT-1 — do the thing\n")
        rc = run_artifact_put(
            argparse.Namespace(phase="plan", slug="llm-config", name="tasks.artifact", file=None)
        )
        assert rc == 0
        from agentalloy.install.subcommands._state import phase_access

        row = phase_access(repo_root).artifact_handle().get_artifact(
            "plan", "llm-config", "tasks.artifact"
        )
        assert row is not None
        assert row["content"] == "# Tasks\n\nT-1 — do the thing\n"

    def test_reads_from_file(
        self, repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = tmp_path / "existing.md"
        source.write_text("# Approach\n\nThe way forward.\n", encoding="utf-8")
        _stdin(monkeypatch, "")
        rc = run_artifact_put(
            argparse.Namespace(
                phase="design", slug="llm-config", name="approach.artifact", file=str(source)
            )
        )
        assert rc == 0
        from agentalloy.install.subcommands._state import phase_access

        row = phase_access(repo_root).artifact_handle().get_artifact(
            "design", "llm-config", "approach.artifact"
        )
        assert row is not None
        assert row["content"] == "# Approach\n\nThe way forward.\n"

    def test_missing_file_fails(
        self, repo_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stdin(monkeypatch, "")
        rc = run_artifact_put(
            argparse.Namespace(
                phase="plan", slug="x", name="tasks.artifact", file=str(repo_root / "nope.md")
            )
        )
        assert rc == 1

    def test_empty_content_fails(self, repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _stdin(monkeypatch, "   \n")
        rc = run_artifact_put(
            argparse.Namespace(phase="plan", slug="x", name="tasks.artifact", file=None)
        )
        assert rc == 1

    def test_prints_confirmation(
        self,
        repo_root: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _stdin(monkeypatch, "# Doc\n\nbody")
        rc = run_artifact_put(
            argparse.Namespace(phase="spec", slug="x", name="spec.artifact", file=None)
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "Recorded spec/x/spec.artifact" in out

    def test_upserts_on_second_put(
        self, repo_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stdin(monkeypatch, "# v1")
        run_artifact_put(
            argparse.Namespace(phase="plan", slug="x", name="tasks.artifact", file=None)
        )
        _stdin(monkeypatch, "# v2")
        rc = run_artifact_put(
            argparse.Namespace(phase="plan", slug="x", name="tasks.artifact", file=None)
        )
        assert rc == 0
        from agentalloy.install.subcommands._state import phase_access

        rows = phase_access(repo_root).artifact_handle().list_artifacts("plan", slug="x")
        assert len(rows) == 1
        assert rows[0]["content"] == "# v2"
