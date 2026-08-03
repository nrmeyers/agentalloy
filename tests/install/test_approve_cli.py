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
    """A spec artifact that also satisfies the spec phase's completeness exit gate."""
    from agentalloy.install.subcommands._state import phase_access

    handle = phase_access(repo_root).contracts_handle()
    handle.set_artifact(
        "spec", "x", "spec.md", "# x\n## Acceptance Criteria\n- a\n## Out of Scope\n- b\n"
    )


def test_approve_writes_marker_and_advances(repo_root: Path) -> None:
    from agentalloy.install.subcommands._state import phase_access

    run_phase_set("spec", root=repo_root)
    _write_spec_doc(repo_root)

    result = run_approve("spec", root=repo_root, approver="alice")

    assert result["ok"] is True
    assert result["marker"] == "state store (approved/spec)"
    approval = phase_access(repo_root).contracts_handle().get_approval("spec")
    assert approval is not None
    assert "artifact_digest" in approval
    assert "approved_at" in approval
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


def test_approve_refuses_without_exit_artifact(repo_root: Path) -> None:
    from agentalloy.install.subcommands._state import phase_access

    run_phase_set("spec", root=repo_root)  # no spec artifact recorded

    result = run_approve("spec", root=repo_root)

    assert result["ok"] is False
    assert "exit artifact" in result["error"]
    assert phase_access(repo_root).contracts_handle().get_approval("spec") is None
    assert run_phase_get(root=repo_root)["phase"] == "spec"


def _write_custom_skill_pack(repo_root: Path) -> None:
    """A custom-skill pack YAML satisfying add-skill's exit-artifact glob."""
    pack = repo_root / ".agentalloy" / "custom-skills" / "my-pack"
    pack.mkdir(parents=True, exist_ok=True)
    (pack / "my-skill.yaml").write_text("skill_id: my-skill\n")


def test_approve_add_skill_writes_marker_and_returns_to_intake(repo_root: Path) -> None:
    run_phase_set("add-skill", root=repo_root)
    _write_custom_skill_pack(repo_root)

    result = run_approve("add-skill", root=repo_root, approver="alice")

    assert result["ok"] is True
    marker = Path(result["marker"])
    assert marker == repo_root / ".agentalloy" / "approved" / "add-skill"
    assert "approver: alice" in marker.read_text()
    # add-skill's deliverable is an installed corpus skill — approval advances
    # back to intake (via _PHASE_GRAPH), not onward through the SDD graph.
    assert result["advanced"]["phase"] == "intake"
    assert run_phase_get(root=repo_root)["phase"] == "intake"


def test_approve_add_skill_refuses_without_scaffolded_pack(repo_root: Path) -> None:
    # The conventional .agentalloy/custom-skills/ location is load-bearing: a
    # pack scaffolded elsewhere leaves the exit-artifact glob empty and approval
    # is refused.
    run_phase_set("add-skill", root=repo_root)

    result = run_approve("add-skill", root=repo_root)

    assert result["ok"] is False
    assert "exit artifact" in result["error"]
    assert not (repo_root / ".agentalloy" / "approved" / "add-skill").exists()
    assert run_phase_get(root=repo_root)["phase"] == "add-skill"


def test_approve_refuses_on_phase_mismatch(repo_root: Path) -> None:
    run_phase_set("build", root=repo_root)
    result = run_approve("spec", root=repo_root)
    assert result["ok"] is False
    assert "not 'spec'" in result["error"]


def test_approve_marker_present_but_forward_completeness_blocks(repo_root: Path) -> None:
    # design → build needs a build contract beyond the design docs. Approval is
    # recorded, but the forward step still reports blocked via `advanced`.
    from agentalloy.install.subcommands._state import phase_access

    run_phase_set("design", root=repo_root)
    handle = phase_access(repo_root).contracts_handle()
    handle.set_artifact("design", "d", "approach.md", "# design\n")

    result = run_approve("design", root=repo_root)

    assert result["ok"] is True  # marker written
    assert phase_access(repo_root).contracts_handle().get_approval("design") is not None
    # The forward write was refused on artifact-completeness, so still in design.
    assert result["advanced"]["blocked"] is True
    assert run_phase_get(root=repo_root)["phase"] == "design"


# --- approve/gate digest agreement -------------------------------------------


def test_approval_globs_match_packs() -> None:
    """`_APPROVAL_STORE_NAME_GLOB[phase]` MUST equal that phase's pack
    `approval_recorded: since_name_glob`.

    They are two sources of truth for one set: `run_approve` digests through the
    map, the gate re-digests through the pack arg. Any drift records a digest the
    gate cannot reproduce, so `approve <phase>` reports success while the phase
    stays blocked — silently and forever.

    This caught design: the split narrowed the pack to approach.md while the map
    still said "*.md", so any repo holding a pre-split tasks.md under
    phase=design would have been permanently unapprovable.
    """
    from agentalloy.signals.gates import (
        _APPROVAL_STORE_NAME_GLOB,  # pyright: ignore[reportPrivateUsage]
    )
    from agentalloy.signals.skill_loader import exit_gates_for_phase

    for phase, mapped in _APPROVAL_STORE_NAME_GLOB.items():
        gate = exit_gates_for_phase(phase)
        assert gate is not None, f"{phase}: no exit gate"
        found = [
            leaf["approval_recorded"].get("since_name_glob")
            for leaf in gate.get("all_of", [])
            if isinstance(leaf, dict) and "approval_recorded" in leaf
        ]
        assert found, f"{phase}: pack declares no approval_recorded leaf"
        assert found[0] == mapped, (
            f"{phase}: pack since_name_glob {found[0]!r} != "
            f"_APPROVAL_STORE_NAME_GLOB {mapped!r} — approve and the gate would "
            f"digest different sets"
        )


def test_plan_is_approvable(repo_root: Path) -> None:
    """`agentalloy approve plan` is a shipped prose invariant of the plan pack, so
    the phase must actually be approvable."""
    from agentalloy.install.subcommands._state import phase_access

    run_phase_set("plan", root=repo_root, force=True)
    handle = phase_access(repo_root).contracts_handle()
    handle.set_artifact("plan", "x", "tasks.md", "# x\n\n## Tasks\n\n- t1\n")
    handle.set_artifact("plan", "x", "test-plan.md", "# x\n\n## Test Cases\n\n- AC-1\n")

    result = run_approve("plan", root=repo_root, approver="alice")

    assert result["ok"] is True
    assert phase_access(repo_root).contracts_handle().get_approval("plan") is not None
