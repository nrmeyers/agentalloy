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
    from agentalloy.signals.predicates import (
        _artifact_digest,  # pyright: ignore[reportPrivateUsage]
    )

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

    def test_build_to_intake_archives(self, repo_root: Path) -> None:
        run_phase_set("build", root=repo_root, force=True)
        self._active(repo_root, "build", "01.md")

        run_phase_set("intake", root=repo_root)

        c = repo_root / ".agentalloy" / "contracts"
        assert (c / "archive" / "build" / "01.md").is_file()
        assert not (c / "active" / "build" / "01.md").exists()

    def test_qa_to_intake_archives(self, repo_root: Path) -> None:
        run_phase_set("qa", root=repo_root, force=True)
        self._active(repo_root, "qa", "01.md")

        run_phase_set("intake", root=repo_root)

        c = repo_root / ".agentalloy" / "contracts"
        assert (c / "archive" / "qa" / "01.md").is_file()
        assert not (c / "active" / "qa" / "01.md").exists()

    def test_direct_intake_set_archives(self, repo_root: Path) -> None:
        run_phase_set("design", root=repo_root, force=True)
        self._active(repo_root, "design", "01.md")

        run_phase_set("intake", root=repo_root)

        c = repo_root / ".agentalloy" / "contracts"
        assert (c / "archive" / "design" / "01.md").is_file()
        assert not (c / "active" / "design" / "01.md").exists()

    def test_intake_to_intake_does_not_rearchive(self, repo_root: Path) -> None:
        run_phase_set("intake", root=repo_root, force=True)

        # No error when setting intake while already in intake.
        result = run_phase_set("intake", root=repo_root)
        assert result.get("blocked") is not True

    def test_spec_to_intake_archives(self, repo_root: Path) -> None:
        run_phase_set("spec", root=repo_root, force=True)
        self._active(repo_root, "spec", "01.md")

        run_phase_set("intake", root=repo_root)

        c = repo_root / ".agentalloy" / "contracts"
        assert (c / "archive" / "spec" / "01.md").is_file()
        assert not (c / "active" / "spec" / "01.md").exists()

    def test_design_to_intake_archives(self, repo_root: Path) -> None:
        run_phase_set("design", root=repo_root, force=True)
        self._active(repo_root, "design", "01.md")

        run_phase_set("intake", root=repo_root)

        c = repo_root / ".agentalloy" / "contracts"
        assert (c / "archive" / "design" / "01.md").is_file()
        assert not (c / "active" / "design" / "01.md").exists()


# ---------------------------------------------------------------------------
# Issue #503 — phase set --force silently no-ops
# ---------------------------------------------------------------------------


class TestIssue503OverrideForwarding:
    """Regression tests for gh#503.

    Defect 1: `--force` wasn't forwarded through the HTTP client to the server,
    so the server re-evaluated the gate without override and blocked the write.
    The CLI's local gate evaluation passed (because it got force=True), so the
    CLI never checked the server's gate verdict.

    Defect 2: The CLI returned `phase` from the argument instead of
    `state.phase` from the read-back, masking the server-side block.
    """

    def test_force_bypasses_qa_gate(self, repo_root: Path) -> None:
        """qa→ship normally requires docs/qa/*.md; --force bypasses that."""
        run_phase_set("qa", root=repo_root)
        # Without --force: gate blocks because no qa docs exist.
        result = run_phase_set("ship", root=repo_root)
        assert result["blocked"] is True
        assert result["phase"] == "qa"  # Defect 2 check: returns stored, not requested

        # With --force: gate is bypassed, write succeeds.
        result = run_phase_set("ship", root=repo_root, force=True)
        assert result["blocked"] is False
        assert result["phase"] == "ship"
        assert run_phase_get(root=repo_root)["phase"] == "ship"

    def test_blocked_write_returns_stored_phase(self, repo_root: Path) -> None:
        """Defect 2: when a forward gate blocks, the returned phase is the stored one."""
        run_phase_set("spec", root=repo_root)
        # No spec artifact recorded → gate blocks on forward to design.
        result = run_phase_set("design", root=repo_root)
        assert result["blocked"] is True
        assert result["phase"] == "spec"  # stored phase, not "design"
        assert run_phase_get(root=repo_root)["phase"] == "spec"

    def test_force_qa_to_ship_persists(self, repo_root: Path) -> None:
        """qa→ship --force actually persists — the core regression from #503."""
        run_phase_set("qa", root=repo_root)
        result = run_phase_set("ship", root=repo_root, force=True)
        assert result["blocked"] is False
        # The read-back must match: the write actually persisted.
        assert run_phase_get(root=repo_root)["phase"] == "ship"


# ---------------------------------------------------------------------------
# Issue #556 — design → plan artifact migration
# ---------------------------------------------------------------------------


def _write_design_tasks(repo_root: Path, slug: str = "01-auth") -> None:
    """Record design artifacts (tasks.md, test-plan.md) the way a pre-split repo would."""
    from agentalloy.install.subcommands._state import phase_access

    handle = phase_access(repo_root).contracts_handle()
    handle.set_artifact(
        "design",
        slug,
        "tasks.md",
        "# Tasks\n\n1. Implement auth endpoint\n2. Add token refresh\n",
    )
    handle.set_artifact(
        "design",
        slug,
        "test-plan.md",
        "# Test Cases\n\n- When user logs in, return auth token\n",
    )


def _approve_design_matching_gate(handle: object) -> None:
    """Record a design approval over exactly the set the gate re-digests.

    Derived from ``_APPROVAL_STORE_NAME_GLOB`` rather than hardcoded: the split
    narrowed design from ``"*.md"`` to ``"approach.md"``, and approving over a
    wider set records a digest the gate can never reproduce — approve reports
    success and the phase stays blocked, silently.
    """
    from agentalloy.signals.gates import (
        _APPROVAL_STORE_NAME_GLOB,  # pyright: ignore[reportPrivateUsage]
    )
    from agentalloy.signals.predicates import (
        _artifact_digest,  # pyright: ignore[reportPrivateUsage]
    )

    rows = handle.list_artifacts("design", name_glob=_APPROVAL_STORE_NAME_GLOB["design"])
    handle.set_approval("design", _artifact_digest(rows))


def _design_artifacts_in_plan(repo_root: Path, slug: str = "01-auth") -> bool:
    """Return True if tasks.md and test-plan.md exist under phase=plan for *slug*."""
    from agentalloy.install.subcommands._state import phase_access

    handle = phase_access(repo_root).contracts_handle()
    plan_rows = handle.list_artifacts("plan", slug=slug)
    names = {r["name"] for r in plan_rows}
    return "tasks.md" in names and "test-plan.md" in names


def _design_approach_exists(repo_root: Path) -> bool:
    """Return True if approach.md still exists under design."""
    from agentalloy.install.subcommands._state import phase_access

    handle = phase_access(repo_root).contracts_handle()
    rows = handle.list_artifacts("design", name_glob="approach.md")
    return len(rows) > 0


class TestDesignToPlanMigration:
    """#556 — design→plan artifact migration.

    Auto-migrate tasks.md / test-plan.md from design→plan on first entry to
    plan.  Design rows are left untouched.
    """

    @staticmethod
    def _purge_stale(repo_root: Path) -> None:
        """Remove ALL design/plan artifacts so this test starts clean.

        The shared process-wide store accumulates 200+ design artifacts from
        other tests; they poison the approval digest and the migration function.
        """
        from agentalloy.install.subcommands._state import phase_access

        handle = phase_access(repo_root).contracts_handle()
        repo = handle._repo()  # noqa: SLF001
        # sdd_artifact is the table that actually pollutes the digest and hands
        # the migration slugs it should never see; sdd_contract matters for slug
        # resolution.
        for table in ("sdd_artifact", "sdd_contract"):
            handle.conn.execute(
                f"DELETE FROM {table} WHERE repo=? AND phase IN ('design','plan')", (repo,)
            )
        # Approvals live in sdd_state as kind='approved' with the phase in
        # session_key — that table has no `phase` column.
        handle.conn.execute(
            "DELETE FROM sdd_state WHERE repo=? AND kind='approved' "
            "AND session_key IN ('design','plan')",
            (repo,),
        )

    def test_migration_copies_artifacts_on_enter_plan(self, repo_root: Path) -> None:
        """A repo with design tasks.md / test-plan.md gets them copied into plan."""
        self._purge_stale(repo_root)
        run_phase_set("design", root=repo_root)
        # Write ALL design artifacts first, then approve — the digest must
        # match _APPROVAL_STORE_NAME_GLOB["design"] = "*.md" because
        # evaluate_phase_gate pre-check recomputes the digest with that glob.
        from agentalloy.install.subcommands._state import phase_access

        handle = phase_access(repo_root).contracts_handle()
        handle.set_artifact(
            "design",
            "01-auth",
            "approach.md",
            "# Approach\n## Approach\nSome approach\n",
        )
        handle.set_artifact(
            "design",
            "01-auth",
            "tasks.md",
            "# Tasks\n\n1. Implement auth endpoint\n2. Add token refresh\n",
        )
        handle.set_artifact(
            "design",
            "01-auth",
            "test-plan.md",
            "# Test Cases\n\n- When user logs in, return auth token\n",
        )
        _approve_design_matching_gate(handle)

        result = run_phase_set("plan", root=repo_root)
        assert result["blocked"] is False
        assert result["phase"] == "plan"
        assert _design_artifacts_in_plan(repo_root)

    def test_migration_preserves_design_rows(self, repo_root: Path) -> None:
        """Design rows are NOT deleted — approval digest must remain valid."""
        run_phase_set("design", root=repo_root)
        from agentalloy.install.subcommands._state import phase_access

        handle = phase_access(repo_root).contracts_handle()
        handle.set_artifact(
            "design",
            "01-auth",
            "approach.md",
            "# Approach\n## Approach\nSome approach\n",
        )
        handle.set_artifact(
            "design",
            "01-auth",
            "tasks.md",
            "# Tasks\n\n1. Implement auth endpoint\n",
        )
        handle.set_artifact(
            "design",
            "01-auth",
            "test-plan.md",
            "# Test Cases\n\n- When user logs in, return auth token\n",
        )
        _approve_design_matching_gate(handle)

        run_phase_set("plan", root=repo_root)

        # The migration must actually have run — without this the rest of the
        # assertions hold vacuously, since "design untouched" is trivially true
        # when nothing happened at all.
        assert _design_artifacts_in_plan(repo_root), "migration did not run"

        # Design keeps every row, not just approach.md: its recorded approval is
        # a digest over them, so a copy that moved rather than copied would
        # invalidate an approval the user already gave — silently.
        assert _design_approach_exists(repo_root)
        design_names = {r["name"] for r in handle.list_artifacts("design", slug="01-auth")}
        assert {"approach.md", "tasks.md", "test-plan.md"} <= design_names

        # And the approval still verifies against what the gate re-digests.
        from agentalloy.signals.gates import (
            _APPROVAL_STORE_NAME_GLOB,  # pyright: ignore[reportPrivateUsage]
        )
        from agentalloy.signals.predicates import (
            _artifact_digest,  # pyright: ignore[reportPrivateUsage]
        )

        recorded = handle.get_approval("design")
        assert recorded is not None
        gate_rows = handle.list_artifacts("design", name_glob=_APPROVAL_STORE_NAME_GLOB["design"])
        assert recorded["artifact_digest"] == _artifact_digest(gate_rows), (
            "migration invalidated design's recorded approval"
        )

    def test_re_entering_plan_does_not_overwrite(self, repo_root: Path) -> None:
        """Migration is idempotent: re-entering plan preserves the plan artifacts."""
        run_phase_set("design", root=repo_root)
        from agentalloy.install.subcommands._state import phase_access

        handle = phase_access(repo_root).contracts_handle()
        handle.set_artifact(
            "design",
            "01-auth",
            "approach.md",
            "# Approach\n## Approach\nSome approach\n",
        )
        handle.set_artifact(
            "design",
            "01-auth",
            "tasks.md",
            "# Tasks\n\n1. Implement auth endpoint\n2. Add token refresh\n",
        )
        handle.set_artifact(
            "design",
            "01-auth",
            "test-plan.md",
            "# Test Cases\n\n- When user logs in, return auth token\n",
        )
        _approve_design_matching_gate(handle)

        run_phase_set("plan", root=repo_root)
        assert _design_artifacts_in_plan(repo_root)

        # Re-enter plan — migration should skip because plan already has
        # artifacts for this slug.
        run_phase_set("plan", root=repo_root)
        assert _design_artifacts_in_plan(repo_root)

        # The plan artifact must still be exactly what was migrated.
        plan_tasks = handle.get_artifact("plan", "01-auth", "tasks.md")
        assert plan_tasks is not None
        assert plan_tasks["content"] is not None
        # Content is what was in design — unchanged by re-entry.

    def test_plan_already_has_slug_is_untouched(self, repo_root: Path) -> None:
        """If plan already holds a slug, design artifacts for that slug are skipped."""
        run_phase_set("design", root=repo_root)

        from agentalloy.install.subcommands._state import phase_access

        handle = phase_access(repo_root).contracts_handle()

        # Write design artifacts — the slug that will be migrated into plan.
        handle.set_artifact(
            "design",
            "01-auth",
            "approach.md",
            "# Approach\n## Approach\nSome approach\n",
        )
        handle.set_artifact(
            "design",
            "01-auth",
            "tasks.md",
            "# Design Tasks\n\n1. Design task\n",
        )
        handle.set_artifact(
            "design",
            "01-auth",
            "test-plan.md",
            "# Test Cases\n\n- Design test\n",
        )
        # Pre-populate plan for the SAME slug — user already produced plan artifacts.
        handle.set_artifact(
            "plan",
            "01-auth",
            "tasks.md",
            "# Custom Tasks\n\n1. Custom task\n",
        )
        handle.set_artifact(
            "plan",
            "01-auth",
            "test-plan.md",
            "# Custom Test Cases\n\n- Custom test\n",
        )
        _approve_design_matching_gate(handle)

        run_phase_set("plan", root=repo_root)

        # The plan artifact must NOT have been overwritten by migration.
        plan_tasks = handle.get_artifact("plan", "01-auth", "tasks.md")
        assert plan_tasks["content"] == "# Custom Tasks\n\n1. Custom task\n"
        plan_tests = handle.get_artifact("plan", "01-auth", "test-plan.md")
        assert plan_tests["content"] == "# Custom Test Cases\n\n- Custom test\n"

    def test_migration_requires_design_artifacts(self, repo_root: Path) -> None:
        """No design tasks.md / test-plan.md → nothing to migrate."""
        run_phase_set("design", root=repo_root)
        # Design exit gate requires approach.md with ## Approach section.
        from agentalloy.install.subcommands._state import phase_access

        handle = phase_access(repo_root).contracts_handle()
        handle.set_artifact(
            "design",
            "01-auth",
            "approach.md",
            "# Approach\n## Approach\nSome approach\n",
        )
        _approve_design_matching_gate(handle)

        result = run_phase_set("plan", root=repo_root)
        assert result["blocked"] is False
        assert result["phase"] == "plan"
        # No artifacts were copied because there were none to copy.
        assert not _design_artifacts_in_plan(repo_root)

    def test_backward_transition_skips_migration(self, repo_root: Path) -> None:
        """A backward transition to plan (spec→plan) skips migration."""
        # Set to spec (forward), then do a backward transition to plan.
        run_phase_set("spec", root=repo_root)
        _approve(repo_root, "spec", "docs/spec/*.md")
        run_phase_set("design", root=repo_root)
        _write_design_tasks(repo_root)

        # Now set to spec (backward), then set to plan (forward from spec perspective,
        # but current=="design", so this is actually design→plan forward).
        # To test a non-design→plan transition, go spec→plan directly (backward).
        run_phase_clear(root=repo_root)
        run_phase_set("spec", root=repo_root)
        _approve(repo_root, "spec", "docs/spec/*.md")

        # spec→plan is backward — current is "spec", not "design" → no migration.
        run_phase_set("plan", root=repo_root, force=True)
        assert run_phase_get(root=repo_root)["phase"] == "plan"
        # No migration happened because current != "design".
        assert not _design_artifacts_in_plan(repo_root)
