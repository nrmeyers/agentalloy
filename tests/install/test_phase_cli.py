"""Unit tests for the ``phase`` subcommand.

Maps to plan: agentalloy phase CLI — set/get/clear phase lock file.
"""

from __future__ import annotations

import argparse
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
    def test_records_the_phase_in_the_store(self, repo_root: Path) -> None:
        result = run_phase_set("build", root=repo_root)
        assert result["phase"] == "build"
        assert run_phase_get(root=repo_root)["phase"] == "build"
        # The row is the only record: no file is written alongside it.
        assert not (repo_root / ".agentalloy" / "phase").exists()

    def test_validates_phase(self, repo_root: Path) -> None:
        with pytest.raises((SystemExit, ValueError)):
            run_phase_set("invalid", root=repo_root)

    def test_valid_phases_accepted(self, repo_root: Path) -> None:
        for phase in ("intake", "spec", "design", "build", "qa", "ship"):
            run_phase_clear(root=repo_root)
            result = run_phase_set(phase, root=repo_root)
            assert result["phase"] == phase

    def test_updates_existing_phase(self, repo_root: Path) -> None:
        run_phase_set("build", root=repo_root)
        original = run_phase_get(root=repo_root)
        run_phase_set("design", root=repo_root)
        updated = run_phase_get(root=repo_root)
        assert updated["phase"] == "design"
        assert updated["started_at"] == original["started_at"]

    def test_creates_no_repo_directory(self, repo_root: Path) -> None:
        """A phase write touches the store and nothing in the repo.

        It used to create ``.agentalloy/`` on the way to writing the phase file.
        Nothing about setting a phase needs a directory now, and creating one
        would put a repo-local artifact back beside the single source of truth.
        """
        assert not (repo_root / ".agentalloy").exists()
        run_phase_set("build", root=repo_root)
        assert not (repo_root / ".agentalloy").exists()


class TestPhaseSetRendering:
    """AC-11 — a successful `phase set` prints, rather than raising afterwards.

    The renderer reads ``result['phase']``.  The migration moved the phase name
    to ``value`` on the wire, and for a while the renderer was reading a key
    ``run_phase_set`` did not return: the write landed, then the command died
    with a ``KeyError``.  The phase had moved and the user saw a traceback.
    """

    def test_set_prints_the_new_phase(
        self, repo_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        args = argparse.Namespace(phase="build", project_root=str(repo_root), force=False)
        assert phase_mod._run_set(args) == 0  # pyright: ignore[reportPrivateUsage]
        assert "Phase set to: build" in capsys.readouterr().out

    def test_get_prints_the_current_phase(
        self, repo_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run_phase_set("design", root=repo_root)
        args = argparse.Namespace(project_root=str(repo_root))
        assert phase_mod._run_get(args) == 0  # pyright: ignore[reportPrivateUsage]
        assert "design" in capsys.readouterr().out


class TestPhaseSetTransitionedBy:
    """Mirrors the proxy's `skill_loader._write_phase_atomic` attribution — lets
    a different session's next turn recognize the phase moved out from under it
    (see `proxy_signal._boundary_confirm_directives`'s swept case)."""

    _SESSION = "11111111-aaaa-4aaa-8aaa-111111111111"

    def test_records_session_on_real_transition(
        self, repo_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", self._SESSION)
        run_phase_set("build", root=repo_root)
        from agentalloy.signals.skill_loader import _read_transitioned_by

        assert _read_transitioned_by(repo_root) == self._SESSION

    def test_no_session_env_records_nothing(
        self, repo_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        run_phase_set("build", root=repo_root)
        from agentalloy.signals.skill_loader import _read_transitioned_by

        assert _read_transitioned_by(repo_root) is None

    def test_idempotent_set_preserves_prior_actor(
        self, repo_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", self._SESSION)
        run_phase_set("build", root=repo_root)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "22222222-bbbb-4bbb-8bbb-222222222222")
        run_phase_set("build", root=repo_root)  # same phase — not a real transition
        from agentalloy.signals.skill_loader import _read_transitioned_by

        assert _read_transitioned_by(repo_root) == self._SESSION


class TestPhaseClear:
    def test_removes_the_phase_row(self, repo_root: Path) -> None:
        run_phase_set("build", root=repo_root)
        assert run_phase_get(root=repo_root)["phase"] == "build"
        run_phase_clear(root=repo_root)
        # Cleared means *absent*, not an empty value: a repo with no phase row
        # is the same state as a freshly wired one.
        assert run_phase_get(root=repo_root)["phase"] is None

    def test_clear_when_no_phase(self, repo_root: Path) -> None:
        result = run_phase_clear(root=repo_root)
        assert result is not None


class TestPhaseRecordShape:
    """What a phase write actually persists, now that it persists to a row."""

    def _row(self, repo_root: Path):
        from agentalloy.install.subcommands._state import phase_access

        return phase_access(repo_root).read()

    def test_metadata_rides_with_the_name(self, repo_root: Path) -> None:
        run_phase_set("build", root=repo_root)
        row = self._row(repo_root)
        assert row.phase == "build"
        assert row.started_at and row.last_updated
        assert row.workflow == "sdd-build"

    def test_blocked_flag_not_persisted(self, repo_root: Path) -> None:
        # `blocked` is a return-only signal about *this call*; it is not state.
        run_phase_set("build", root=repo_root)
        row = self._row(repo_root)
        assert not hasattr(row, "blocked")


def _write_spec_doc(repo_root: Path) -> None:
    """Record a spec artifact that satisfies the `spec` phase's exit gate."""
    from agentalloy.install.subcommands._state import phase_access

    handle = phase_access(repo_root).contracts_handle()
    handle.set_artifact(
        "spec", "x", "spec.md", "# x\n## Acceptance Criteria\n- a\n## Out of Scope\n- b\n"
    )


def _approve(repo_root: Path, phase: str, since_glob: str) -> None:
    """Record a fresh approval for `phase`, matching its current artifact digest.

    `since_glob` is retained as a param for call-site parity with the pre-migration
    signature; store-backed phases (spec/design) ignore it and digest all `*.md`
    artifacts instead.
    """
    from agentalloy.install.subcommands._state import phase_access
    from agentalloy.signals.predicates import _artifact_digest  # pyright: ignore[reportPrivateUsage]

    handle = phase_access(repo_root).contracts_handle()
    rows = handle.list_artifacts(phase, name_glob="*.md")
    handle.set_approval(phase, _artifact_digest(rows))


class TestGuardedAdvance:
    """B2 — a *forward* `phase set` is gated on the current phase's exit gate.

    Maps to test-plan TC15–TC20.
    """

    def test_forward_guard_blocks_when_artifact_missing(self, repo_root: Path) -> None:
        # TC15: in `spec` with no recorded artifact → spec→design refuses.
        run_phase_set("spec", root=repo_root)
        result = run_phase_set("design", root=repo_root)
        assert result["blocked"] is True
        assert result["phase"] == "spec"  # unchanged
        assert result["target"] == "design"
        assert any("artifact-set --phase spec" in a for a in result["advisories"])
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

        run_phase_clear(root=repo_root)
        run_phase_set("design", root=repo_root)
        assert run_phase_set("spec", root=repo_root)["phase"] == "spec"

        run_phase_clear(root=repo_root)
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

        # This gate is a disk-based fake, deliberately independent of the real
        # (now store-backed) spec pack — it isolates the deterministic-vs-UNKNOWN
        # forward-gate behavior from the artifact-store migration. The approval
        # half of `evaluate_phase_gate` is store-backed unconditionally for
        # "spec", so `_write_spec_doc`/`_approve` (store-backed) are still used
        # for that half; the disk file below satisfies only this fake gate.
        gate = {
            "all_of": [
                {"artifact_exists": {"path": "docs/spec/*.md"}},
                {"artifact_completeness": {"path": "docs/spec/*.md", "criteria": "thorough"}},
            ]
        }
        monkeypatch.setattr(skill_loader, "exit_gates_for_phase", lambda _phase: gate)
        spec_dir = repo_root / "docs" / "spec"

        # deterministic part MET, semantic part UNKNOWN → allowed (UNKNOWN doesn't block)
        run_phase_set("spec", root=repo_root)
        spec_dir.mkdir(parents=True, exist_ok=True)
        (spec_dir / "x.md").write_text("# x\n")
        _write_spec_doc(repo_root)
        _approve(repo_root, "spec", "docs/spec/*.md")  # #10: clear the approval gate
        assert run_phase_set("design", root=repo_root)["blocked"] is False

        # deterministic part NOT_MET → blocked, regardless of the UNKNOWN semantic part
        run_phase_clear(root=repo_root)
        for p in spec_dir.glob("*.md"):
            p.unlink()
        run_phase_set("spec", root=repo_root)
        assert run_phase_set("design", root=repo_root)["blocked"] is True

    def test_phase_to_gate_loader_is_corpus_free(self, repo_root: Path) -> None:
        # TC20: each phase maps to its packaged exit_gates, read from the wheel
        # YAML with no corpus/DB present (repo_root has no .duckdb / LadybugDB).
        from agentalloy.signals.skill_loader import exit_gates_for_phase

        spec_gate = exit_gates_for_phase("spec")
        assert spec_gate is not None
        assert "'phase': 'spec'" in str(spec_gate)

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
        # Approval recorded but the spec artifact is missing its required sections:
        # --force waives the completeness gate and advances.
        from agentalloy.install.subcommands._state import phase_access

        run_phase_set("spec", root=repo_root)
        handle = phase_access(repo_root).contracts_handle()
        handle.set_artifact("spec", "x", "spec.md", "# spec only, no required sections\n")
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
        # gate drives the "record its exit artifact" message (no reason='approval').
        run_phase_set("spec", root=repo_root)
        result = run_phase_set("design", root=repo_root)
        assert result["blocked"] is True
        assert result.get("reason") != "approval"
        assert any("artifact-set --phase spec" in a for a in result["advisories"])


class TestShipResetAutoArchive:
    def _active(self, root: Path, phase: str, name: str) -> Path:
        p = root / ".agentalloy" / "contracts" / "active" / phase / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"---\nphase: {phase}\ntask_slug: {p.stem}\ndomain_tags: [x]\n---\nbody\n")
        return p

    def test_ship_to_intake_reset_archives_live_contracts(self, repo_root: Path) -> None:
        run_phase_set("ship", root=repo_root, force=True)
        self._active(repo_root, "build", "01.md")
        self._active(repo_root, "ship", "s.md")

        run_phase_set("intake", root=repo_root)  # user-confirmed reset

        c = repo_root / ".agentalloy" / "contracts"
        # Live cycle swept into archive/<phase>/ …
        assert (c / "archive" / "build" / "01.md").is_file()
        assert (c / "archive" / "ship" / "s.md").is_file()
        # … and no longer live.
        assert not (c / "active" / "build" / "01.md").exists()
        assert not (c / "active" / "ship" / "s.md").exists()

    def test_non_ship_transition_does_not_archive(self, repo_root: Path) -> None:
        run_phase_set("build", root=repo_root, force=True)
        self._active(repo_root, "build", "01.md")

        run_phase_set("qa", root=repo_root, force=True)

        # A normal forward transition leaves live contracts in place.
        assert (repo_root / ".agentalloy" / "contracts" / "active" / "build" / "01.md").is_file()
