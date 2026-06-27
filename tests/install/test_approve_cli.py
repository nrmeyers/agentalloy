"""Unit tests for the ``approve`` subcommand (#10 — human-approval gate)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentalloy.install.subcommands.approve import run_approve
from agentalloy.install.subcommands.phase import run_phase_get, run_phase_set


@pytest.fixture()
def repo_root(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text("")
    return tmp_path


def _write_spec_doc(repo_root: Path) -> None:
    """A spec doc that also satisfies the spec phase's completeness exit gate."""
    spec = repo_root / "docs" / "spec"
    spec.mkdir(parents=True, exist_ok=True)
    (spec / "x.md").write_text("# x\n## Acceptance Criteria\n- a\n## Out of Scope\n- b\n")


def test_approve_writes_marker_and_advances(repo_root: Path) -> None:
    run_phase_set("spec", root=repo_root)
    _write_spec_doc(repo_root)

    result = run_approve("spec", root=repo_root, approver="alice")

    assert result["ok"] is True
    marker = Path(result["marker"])
    assert marker == repo_root / ".agentalloy" / "approved" / "spec"
    content = marker.read_text()
    assert "approver: alice" in content
    assert "approved_at:" in content
    assert "artifact_sha256:" in content
    # The marker write auto-advances the phase to design.
    assert result["advanced"]["phase"] == "design"
    assert run_phase_get(root=repo_root)["phase"] == "design"


def test_approve_records_default_approver_from_env(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_phase_set("spec", root=repo_root)
    _write_spec_doc(repo_root)
    monkeypatch.setenv("USER", "bob")
    result = run_approve("spec", root=repo_root)
    assert result["approver"] == "bob"
    assert "approver: bob" in Path(result["marker"]).read_text()


def test_approve_refuses_without_exit_artifact(repo_root: Path) -> None:
    run_phase_set("spec", root=repo_root)  # no docs/spec/*.md produced

    result = run_approve("spec", root=repo_root)

    assert result["ok"] is False
    assert "exit artifact" in result["error"]
    assert not (repo_root / ".agentalloy" / "approved" / "spec").exists()
    assert run_phase_get(root=repo_root)["phase"] == "spec"


def test_approve_refuses_on_phase_mismatch(repo_root: Path) -> None:
    run_phase_set("build", root=repo_root)
    result = run_approve("spec", root=repo_root)
    assert result["ok"] is False
    assert "not 'spec'" in result["error"]
    assert not (repo_root / ".agentalloy" / "approved" / "spec").exists()


def test_approve_marker_present_but_forward_completeness_blocks(repo_root: Path) -> None:
    # design → build needs a build contract beyond the design docs. Approval is
    # recorded, but the forward step still reports blocked via `advanced`.
    run_phase_set("design", root=repo_root)
    design = repo_root / "docs" / "design"
    design.mkdir(parents=True, exist_ok=True)
    (design / "d.md").write_text("# design\n")

    result = run_approve("design", root=repo_root)

    assert result["ok"] is True  # marker written
    assert (repo_root / ".agentalloy" / "approved" / "design").exists()
    # The forward write was refused on artifact-completeness, so still in design.
    assert result["advanced"]["blocked"] is True
    assert run_phase_get(root=repo_root)["phase"] == "design"
