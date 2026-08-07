"""``agentalloy workflow`` CLI — pause/resume/status happy paths + idempotency.

The workflow verbs are deterministic per-repo phase-row edits (like ``phase
set``): they never touch the ``phase`` value, and resume restores exactly the
prior phase. Follows the ``phase`` CLI test conventions (direct run_* calls with
an explicit root, plus a dispatcher pass through ``main``).
"""

from __future__ import annotations

from pathlib import Path

from agentalloy.install.__main__ import main
from agentalloy.install.subcommands.phase import run_phase_set
from agentalloy.install.subcommands.workflow import (
    run_workflow_pause,
    run_workflow_resume,
    run_workflow_status,
)
from agentalloy.signals.skill_loader import (  # pyright: ignore[reportPrivateUsage]
    _read_phase,
    read_pause_state,
)


class TestWorkflowPause:
    def test_pause_sets_mode_and_keeps_phase(self, tmp_path: Path) -> None:
        run_phase_set("design", root=tmp_path, force=True)
        result = run_workflow_pause(root=tmp_path)
        assert result["changed"] is True
        assert result["phase"] == "design"
        assert result["paused_since"]
        assert _read_phase(tmp_path) == "design"
        mode, since = read_pause_state(tmp_path)
        assert mode == "paused" and since == result["paused_since"]

    def test_pause_is_idempotent(self, tmp_path: Path) -> None:
        run_phase_set("build", root=tmp_path, force=True)
        first = run_workflow_pause(root=tmp_path)
        second = run_workflow_pause(root=tmp_path)
        assert second["changed"] is False
        # paused_since is NOT reset by a repeat — the 24h reminder clock holds.
        assert second["paused_since"] == first["paused_since"]

    def test_pause_without_phase_creates_intake(self, tmp_path: Path) -> None:
        result = run_workflow_pause(root=tmp_path)
        assert result["phase"] == "intake"
        assert read_pause_state(tmp_path)[0] == "paused"


def _row(root: Path):
    """The stored phase record for *root* — the workflow verbs' only persistence."""
    from agentalloy.install.subcommands._state import phase_access

    return phase_access(root).read()


class TestWorkflowResume:
    def test_resume_restores_exact_phase(self, tmp_path: Path) -> None:
        run_phase_set("qa", root=tmp_path, force=True)
        run_workflow_pause(root=tmp_path)
        result = run_workflow_resume(root=tmp_path)
        assert result == {"phase": "qa", "mode": "workflow", "changed": True}
        assert _read_phase(tmp_path) == "qa"
        assert read_pause_state(tmp_path) == ("workflow", None)
        # Cleared, not merely falsy: the stored row carries no pause stamp.
        row = _row(tmp_path)
        assert row is not None
        assert not row.paused_since

    def test_resume_is_idempotent(self, tmp_path: Path) -> None:
        run_phase_set("build", root=tmp_path, force=True)
        result = run_workflow_resume(root=tmp_path)
        assert result["changed"] is False
        assert result["phase"] == "build"

    def test_resume_preserves_other_phase_fields(self, tmp_path: Path) -> None:
        run_phase_set("spec", root=tmp_path, force=True)
        before = _row(tmp_path)
        run_workflow_pause(root=tmp_path)
        run_workflow_resume(root=tmp_path)
        after = _row(tmp_path)
        assert before is not None and after is not None
        # A pause/resume round trip touches mode and paused_since and nothing else.
        assert (after.phase, after.started_at, after.workflow) == (
            before.phase,
            before.started_at,
            before.workflow,
        )


class TestWorkflowStatus:
    def test_status_workflow(self, tmp_path: Path) -> None:
        run_phase_set("build", root=tmp_path, force=True)
        assert run_workflow_status(root=tmp_path) == {
            "phase": "build",
            "mode": "workflow",
            "paused_since": None,
        }

    def test_status_paused(self, tmp_path: Path) -> None:
        run_phase_set("build", root=tmp_path, force=True)
        entered = run_workflow_pause(root=tmp_path)
        status = run_workflow_status(root=tmp_path)
        assert status["mode"] == "paused"
        assert status["phase"] == "build"
        assert status["paused_since"] == entered["paused_since"]

    def test_status_no_phase_file(self, tmp_path: Path) -> None:
        assert run_workflow_status(root=tmp_path) == {
            "phase": None,
            "mode": "workflow",
            "paused_since": None,
        }


class TestWorkflowDispatcher:
    def test_main_pause_resume_status(self, tmp_path: Path, capsys: object) -> None:
        root = ["--project-root", str(tmp_path)]
        assert main(["phase", "set", "intake", *root]) == 0
        assert main(["workflow", "pause", *root]) == 0
        assert read_pause_state(tmp_path)[0] == "paused"
        assert main(["workflow", "status", *root]) == 0
        assert main(["workflow", "resume", *root]) == 0
        assert read_pause_state(tmp_path) == ("workflow", None)
        # Bare `workflow` defaults to status.
        assert main(["workflow", *root]) == 0
