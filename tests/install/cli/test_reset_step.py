"""Unit tests for the ``reset-step`` subcommand."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentalloy.install import state as install_state
from agentalloy.install.subcommands.reset_step import (
    _dependents_of,  # pyright: ignore[reportPrivateUsage]
    reset_step,
)


@pytest.fixture()
def repo_root(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text("")
    return tmp_path


def _setup_completed(root: Path, steps: list[str]) -> None:
    st = install_state.load_state(root)
    for step in steps:
        st = install_state.record_step(st, step)
    install_state.save_state(st, root)


class TestDependents:
    def test_detect_dependents(self) -> None:
        deps = _dependents_of("detect")
        assert "recommend-host-targets" in deps
        assert "recommend-models" in deps

    def test_write_env_dependents(self) -> None:
        deps = _dependents_of("write-env")
        assert "wire-harness" in deps
        assert "verify" in deps

    def test_seed_corpus_dependents(self) -> None:
        deps = _dependents_of("seed-corpus")
        assert "verify" in deps

    def test_verify_no_dependents(self) -> None:
        deps = _dependents_of("verify")
        assert deps == set()


class TestResetStep:
    def test_clears_step(self, repo_root: Path) -> None:
        _setup_completed(repo_root, ["detect", "recommend-host-targets"])
        result = reset_step("detect", root=repo_root)
        assert result["step_cleared"] == "detect"
        st = install_state.load_state(repo_root)
        assert not install_state.is_step_completed(st, "detect")

    def test_clears_dependents(self, repo_root: Path) -> None:
        _setup_completed(repo_root, ["detect", "recommend-host-targets", "recommend-models"])
        result = reset_step("detect", root=repo_root)
        assert "recommend-host-targets" in result["dependent_steps_also_cleared"]
        assert "recommend-models" in result["dependent_steps_also_cleared"]
        st = install_state.load_state(repo_root)
        assert not install_state.is_step_completed(st, "recommend-host-targets")
        assert not install_state.is_step_completed(st, "recommend-models")

    def test_unknown_step_exits(self, repo_root: Path) -> None:
        with pytest.raises(SystemExit):
            reset_step("nonexistent", root=repo_root)

    def test_step_not_completed_exits(self, repo_root: Path) -> None:
        with pytest.raises(SystemExit, match="4"):
            reset_step("detect", root=repo_root)

    def test_preserves_independent_steps(self, repo_root: Path) -> None:
        _setup_completed(repo_root, ["detect", "seed-corpus"])
        reset_step("detect", root=repo_root)
        st = install_state.load_state(repo_root)
        assert install_state.is_step_completed(st, "seed-corpus")

    def test_output_schema(self, repo_root: Path) -> None:
        _setup_completed(repo_root, ["detect"])
        result = reset_step("detect", root=repo_root)
        assert result["schema_version"] == 1
        assert "step_cleared" in result
        assert "dependent_steps_also_cleared" in result

    def test_clears_top_level_state_keys(self, repo_root: Path) -> None:
        """reset-step must also clear top-level state fields owned by the step,
        otherwise re-running the step reads stale data and either falsely
        errors via tamper-detection or skips writing the expected target."""
        _setup_completed(repo_root, ["detect", "write-env", "wire-harness"])
        st = install_state.load_state(repo_root)
        st["harness"] = "claude-code"
        st["harness_files_written"] = [{"path": str(repo_root / "CLAUDE.md")}]
        st["env_path"] = str(repo_root / ".env")
        st["port"] = 8000
        install_state.save_state(st, repo_root)

        result = reset_step("write-env", root=repo_root)
        st = install_state.load_state(repo_root)
        # write-env owns env_path + port; wire-harness (dependent) owns harness fields.
        assert "env_path" not in st
        assert "port" not in st
        assert "harness" not in st
        assert "harness_files_written" not in st
        assert "env_path" in result["state_keys_cleared"]
