"""Unit tests for the ``phase`` subcommand.

Maps to plan: agentalloy phase CLI — set/get/clear phase lock file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentalloy.install.subcommands.phase import (
    run_phase_clear,
    run_phase_get,
    run_phase_set,
)


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
