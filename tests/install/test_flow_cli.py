"""``agentalloy flow`` CLI — free/resume/status happy paths + idempotency.

The flow verbs are deterministic per-repo phase-file edits (like ``phase set``):
they never touch the ``phase`` value, and resume restores exactly the prior
phase. Follows the ``phase`` CLI test conventions (direct run_* calls with an
explicit root, plus a dispatcher pass through ``main``).
"""

from __future__ import annotations

from pathlib import Path

from agentalloy.install.__main__ import main
from agentalloy.install.subcommands.flow import (
    run_flow_free,
    run_flow_resume,
    run_flow_status,
)
from agentalloy.install.subcommands.phase import run_phase_set
from agentalloy.signals.skill_loader import (  # pyright: ignore[reportPrivateUsage]
    _read_phase,
    read_flow_state,
)


class TestFlowFree:
    def test_free_sets_mode_and_keeps_phase(self, tmp_path: Path) -> None:
        run_phase_set("design", root=tmp_path, force=True)
        result = run_flow_free(root=tmp_path)
        assert result["changed"] is True
        assert result["phase"] == "design"
        assert result["free_since"]
        assert _read_phase(tmp_path) == "design"
        mode, since = read_flow_state(tmp_path)
        assert mode == "free" and since == result["free_since"]

    def test_free_is_idempotent(self, tmp_path: Path) -> None:
        run_phase_set("build", root=tmp_path, force=True)
        first = run_flow_free(root=tmp_path)
        second = run_flow_free(root=tmp_path)
        assert second["changed"] is False
        # free_since is NOT reset by a repeat — the 24h reminder clock holds.
        assert second["free_since"] == first["free_since"]

    def test_free_without_phase_file_creates_intake(self, tmp_path: Path) -> None:
        result = run_flow_free(root=tmp_path)
        assert result["phase"] == "intake"
        assert read_flow_state(tmp_path)[0] == "free"


class TestFlowResume:
    def test_resume_restores_exact_phase(self, tmp_path: Path) -> None:
        run_phase_set("qa", root=tmp_path, force=True)
        run_flow_free(root=tmp_path)
        result = run_flow_resume(root=tmp_path)
        assert result == {"phase": "qa", "mode": "workflow", "changed": True}
        assert _read_phase(tmp_path) == "qa"
        assert read_flow_state(tmp_path) == ("workflow", None)
        # The free-flow fields are fully gone from the file.
        raw = (tmp_path / ".agentalloy" / "phase").read_text()
        assert "mode:" not in raw and "free_since:" not in raw

    def test_resume_is_idempotent(self, tmp_path: Path) -> None:
        run_phase_set("build", root=tmp_path, force=True)
        result = run_flow_resume(root=tmp_path)
        assert result["changed"] is False
        assert result["phase"] == "build"

    def test_resume_preserves_other_phase_fields(self, tmp_path: Path) -> None:
        run_phase_set("spec", root=tmp_path, force=True)
        before = (tmp_path / ".agentalloy" / "phase").read_text()
        run_flow_free(root=tmp_path)
        run_flow_resume(root=tmp_path)
        after = (tmp_path / ".agentalloy" / "phase").read_text()
        assert after == before  # started_at / last_updated / workflow survive


class TestFlowStatus:
    def test_status_workflow(self, tmp_path: Path) -> None:
        run_phase_set("build", root=tmp_path, force=True)
        assert run_flow_status(root=tmp_path) == {
            "phase": "build",
            "mode": "workflow",
            "free_since": None,
        }

    def test_status_free(self, tmp_path: Path) -> None:
        run_phase_set("build", root=tmp_path, force=True)
        entered = run_flow_free(root=tmp_path)
        status = run_flow_status(root=tmp_path)
        assert status["mode"] == "free"
        assert status["phase"] == "build"
        assert status["free_since"] == entered["free_since"]

    def test_status_no_phase_file(self, tmp_path: Path) -> None:
        assert run_flow_status(root=tmp_path) == {
            "phase": None,
            "mode": "workflow",
            "free_since": None,
        }


class TestFlowDispatcher:
    def test_main_free_resume_status(self, tmp_path: Path, capsys: object) -> None:
        root = ["--project-root", str(tmp_path)]
        assert main(["phase", "set", "intake", *root]) == 0
        assert main(["flow", "free", *root]) == 0
        assert read_flow_state(tmp_path)[0] == "free"
        assert main(["flow", "status", *root]) == 0
        assert main(["flow", "resume", *root]) == 0
        assert read_flow_state(tmp_path) == ("workflow", None)
        # Bare `flow` defaults to status.
        assert main(["flow", *root]) == 0
