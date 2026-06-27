"""Unit tests for the ``phase`` subcommand.

Maps to plan: agentalloy phase CLI — set/get/clear phase lock file.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import pytest

from agentalloy.install.subcommands import phase as phase_mod
from agentalloy.install.subcommands.phase import (
    run_phase_clear,
    run_phase_get,
    run_phase_set,
)


class TestPhaseSubcommandParsing:
    """Argparse-level: `phase get` must be a real subcommand.

    Regression: only `set`/`clear` were registered, so an explicit
    `agentalloy phase get` (the natural read verb agents reach for) errored
    with `invalid choice: 'get'`, even though bare `phase` defaulted to get.
    """

    @staticmethod
    def _parser() -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(prog="agentalloy")
        sub = parser.add_subparsers()
        phase_mod.add_parser(sub)
        return parser

    def test_get_is_a_valid_subcommand(self) -> None:
        args = self._parser().parse_args(["phase", "get"])
        assert args.func is phase_mod._run_get  # pyright: ignore[reportPrivateUsage]

    def test_bare_phase_defaults_to_get(self) -> None:
        args = self._parser().parse_args(["phase"])
        assert args.func is phase_mod._run_get  # pyright: ignore[reportPrivateUsage]

    def test_set_and_clear_still_parse(self) -> None:
        parser = self._parser()
        assert parser.parse_args(["phase", "set", "spec"]).func is phase_mod._run_set  # pyright: ignore[reportPrivateUsage]
        assert parser.parse_args(["phase", "clear"]).func is phase_mod._run_clear  # pyright: ignore[reportPrivateUsage]


@pytest.fixture()
def repo_root(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text("")
    return tmp_path


class TestPhaseGet:
    def test_no_phase_returns_none(self, repo_root: Path) -> None:
        result = run_phase_get(root=repo_root)
        assert result.get("phase") is None

    def test_returns_current_phase(self, repo_root: Path) -> None:
        run_phase_set("build", root=repo_root)
        result = run_phase_get(root=repo_root)
        assert result["phase"] == "build"

    def test_returns_full_info(self, repo_root: Path) -> None:
        run_phase_set("design", root=repo_root)
        result = run_phase_get(root=repo_root)
        assert result["phase"] == "design"
        assert "started_at" in result
        assert "last_updated" in result
        assert "workflow" in result


class TestPhaseSet:
    def test_creates_phase_file(self, repo_root: Path) -> None:
        result = run_phase_set("build", root=repo_root)
        phase_file = repo_root / ".agentalloy" / "phase"
        assert phase_file.exists()
        assert result["phase"] == "build"

    def test_validates_phase(self, repo_root: Path) -> None:
        with pytest.raises((SystemExit, ValueError)):
            run_phase_set("invalid", root=repo_root)

    def test_valid_phases_accepted(self, repo_root: Path) -> None:
        for phase in ("intake", "spec", "design", "build", "qa", "ship"):
            (repo_root / ".agentalloy" / "phase").unlink(missing_ok=True)
            result = run_phase_set(phase, root=repo_root)
            assert result["phase"] == phase

    def test_updates_existing_phase(self, repo_root: Path) -> None:
        run_phase_set("build", root=repo_root)
        original = run_phase_get(root=repo_root)
        run_phase_set("design", root=repo_root)
        updated = run_phase_get(root=repo_root)
        assert updated["phase"] == "design"
        assert updated["started_at"] == original["started_at"]

    def test_creates_directory(self, repo_root: Path) -> None:
        assert not (repo_root / ".agentalloy").exists()
        run_phase_set("build", root=repo_root)
        assert (repo_root / ".agentalloy").is_dir()


class TestPhaseClear:
    def test_removes_phase_file(self, repo_root: Path) -> None:
        run_phase_set("build", root=repo_root)
        assert (repo_root / ".agentalloy" / "phase").exists()
        run_phase_clear(root=repo_root)
        assert not (repo_root / ".agentalloy" / "phase").exists()

    def test_clear_when_no_phase(self, repo_root: Path) -> None:
        result = run_phase_clear(root=repo_root)
        assert result is not None


class TestPhaseFileFormat:
    def test_yaml_format(self, repo_root: Path) -> None:
        run_phase_set("build", root=repo_root)
        content = (repo_root / ".agentalloy" / "phase").read_text()
        assert "phase: build" in content
        assert "started_at:" in content
        assert "last_updated:" in content
        assert "workflow:" in content

    def test_blocked_flag_not_persisted(self, repo_root: Path) -> None:
        # The return-only `blocked` signal must never leak into the lock file.
        run_phase_set("build", root=repo_root)
        assert "blocked" not in (repo_root / ".agentalloy" / "phase").read_text()


def _write_spec_doc(repo_root: Path) -> None:
    """Write a spec doc that satisfies the `spec` phase's exit gate."""
    spec = repo_root / "docs" / "spec"
    spec.mkdir(parents=True, exist_ok=True)
    (spec / "x.md").write_text("# x\n## Acceptance Criteria\n- a\n## Out of Scope\n- b\n")


def _approve(repo_root: Path, phase: str, since_glob: str) -> None:
    """Write a fresh approval marker for `phase`, newer than its exit artifact."""
    marker = repo_root / ".agentalloy" / "approved" / phase
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text('approver: t\napproved_at: "2026-01-01T00:00:00Z"\nartifact_sha256: x\n')
    base = max((p.stat().st_mtime for p in repo_root.glob(since_glob) if p.is_file()), default=0.0)
    os.utime(marker, (base + 10, base + 10))


class TestGuardedAdvance:
    """B2 — a *forward* `phase set` is gated on the current phase's exit gate.

    Maps to test-plan TC15–TC20.
    """

    def test_forward_guard_blocks_when_artifact_missing(self, repo_root: Path) -> None:
        # TC15: in `spec` with no docs/spec/*.md → spec→design refuses.
        run_phase_set("spec", root=repo_root)
        result = run_phase_set("design", root=repo_root)
        assert result["blocked"] is True
        assert result["phase"] == "spec"  # unchanged
        assert result["target"] == "design"
        assert any("docs/spec" in a for a in result["advisories"])
        # phase file still says spec
        assert run_phase_get(root=repo_root)["phase"] == "spec"

    def test_forward_guard_passes_when_artifact_present(self, repo_root: Path) -> None:
        # TC16: conformant spec doc present + approval recorded → spec→design succeeds.
        run_phase_set("spec", root=repo_root)
        _write_spec_doc(repo_root)
        _approve(repo_root, "spec", "docs/spec/*.md")  # #10: spec→design now needs approval
        result = run_phase_set("design", root=repo_root)
        assert result["blocked"] is False
        assert result["phase"] == "design"

    def test_force_bypasses_the_gate(self, repo_root: Path) -> None:
        # TC17: --force writes regardless of the gate.
        run_phase_set("spec", root=repo_root)
        result = run_phase_set("design", root=repo_root, force=True)
        assert result["blocked"] is False
        assert result["phase"] == "design"

    def test_backward_and_bail_are_unguarded(self, repo_root: Path) -> None:
        # TC18: backward (qa→build, design→spec) and bail (sdd-fast→spec) never gate.
        run_phase_set("qa", root=repo_root)
        assert run_phase_set("build", root=repo_root)["phase"] == "build"

        (repo_root / ".agentalloy" / "phase").unlink()
        run_phase_set("design", root=repo_root)
        assert run_phase_set("spec", root=repo_root)["phase"] == "spec"

        (repo_root / ".agentalloy" / "phase").unlink()
        run_phase_set("sdd-fast", root=repo_root)
        assert run_phase_set("spec", root=repo_root)["phase"] == "spec"

    def test_ship_to_intake_reset_is_unguarded(self, repo_root: Path) -> None:
        # TC23 (partial): the ship→intake reset is not a linear-forward edge → unguarded.
        run_phase_set("ship", root=repo_root)
        assert run_phase_set("intake", root=repo_root)["phase"] == "intake"

    def test_unknown_never_blocks_only_not_met_does(
        self, repo_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # TC19: guard evaluates deterministically (lm_client=None). A semantic
        # predicate yields UNKNOWN and must NOT block; only a deterministic
        # NOT_MET blocks.
        import agentalloy.signals.skill_loader as skill_loader

        gate = {
            "all_of": [
                {"artifact_exists": {"path": "docs/spec/*.md"}},
                {"artifact_completeness": {"path": "docs/spec/*.md", "criteria": "thorough"}},
            ]
        }
        monkeypatch.setattr(skill_loader, "exit_gates_for_phase", lambda _phase: gate)

        # deterministic part MET, semantic part UNKNOWN → allowed (UNKNOWN doesn't block)
        run_phase_set("spec", root=repo_root)
        _write_spec_doc(repo_root)
        _approve(repo_root, "spec", "docs/spec/*.md")  # #10: clear the approval gate
        assert run_phase_set("design", root=repo_root)["blocked"] is False

        # deterministic part NOT_MET → blocked, regardless of the UNKNOWN semantic part
        (repo_root / ".agentalloy" / "phase").unlink()
        for p in (repo_root / "docs" / "spec").glob("*.md"):
            p.unlink()
        run_phase_set("spec", root=repo_root)
        assert run_phase_set("design", root=repo_root)["blocked"] is True

    def test_phase_to_gate_loader_is_corpus_free(self, repo_root: Path) -> None:
        # TC20: each phase maps to its packaged exit_gates, read from the wheel
        # YAML with no corpus/DB present (repo_root has no .duckdb / LadybugDB).
        from agentalloy.signals.skill_loader import exit_gates_for_phase

        spec_gate = exit_gates_for_phase("spec")
        assert spec_gate is not None
        assert "docs/spec" in str(spec_gate)

        for phase in ("intake", "spec", "design", "build", "qa", "ship", "sdd-fast"):
            assert exit_gates_for_phase(phase) is not None


class TestApprovalGate:
    """#10 — the human-approval gate that ``--force`` must NOT bypass.

    spec→design / design→build on the full lane require a recorded approval
    marker. ``--force`` waives artifact-completeness but never the human
    checkpoint.
    """

    def test_force_does_not_bypass_approval(self, repo_root: Path) -> None:
        # Exit artifact present and complete, but no approval marker: even
        # --force is refused, with reason='approval'.
        run_phase_set("spec", root=repo_root)
        _write_spec_doc(repo_root)
        result = run_phase_set("design", root=repo_root, force=True)
        assert result["blocked"] is True
        assert result["reason"] == "approval"
        assert result["phase"] == "spec"  # unchanged
        assert result["target"] == "design"
        assert any("approve spec" in a for a in result["advisories"])
        assert run_phase_get(root=repo_root)["phase"] == "spec"

    def test_force_bypasses_completeness_not_approval(self, repo_root: Path) -> None:
        # Approval recorded but the spec doc is missing its required sections:
        # --force waives the completeness gate and advances.
        run_phase_set("spec", root=repo_root)
        spec = repo_root / "docs" / "spec"
        spec.mkdir(parents=True, exist_ok=True)
        (spec / "x.md").write_text("# spec only, no required sections\n")
        _approve(repo_root, "spec", "docs/spec/*.md")
        result = run_phase_set("design", root=repo_root, force=True)
        assert result["blocked"] is False
        assert result["phase"] == "design"

    def test_present_but_unapproved_blocks_without_force(self, repo_root: Path) -> None:
        # Complete spec, no approval, no force → blocked on approval (not completeness).
        run_phase_set("spec", root=repo_root)
        _write_spec_doc(repo_root)
        result = run_phase_set("design", root=repo_root)
        assert result["blocked"] is True
        assert result["reason"] == "approval"

    def test_missing_artifact_defers_to_completeness_gate(self, repo_root: Path) -> None:
        # No exit artifact at all → approval gate steps aside; the completeness
        # gate drives the "produce docs/spec" message (no reason='approval').
        run_phase_set("spec", root=repo_root)
        result = run_phase_set("design", root=repo_root)
        assert result["blocked"] is True
        assert result.get("reason") != "approval"
        assert any("docs/spec" in a for a in result["advisories"])
