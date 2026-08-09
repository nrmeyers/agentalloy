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
        "spec", "x", "spec.artifact", "# x\n## Acceptance Criteria\n- a\n## Out of Scope\n- b\n"
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
    handle.set_artifact("design", "d", "approach.artifact", "# design\n")

    result = run_approve("design", root=repo_root)

    assert result["ok"] is True  # marker written
    assert phase_access(repo_root).contracts_handle().get_approval("design") is not None
    # The forward write was refused on artifact-completeness, so still in design.
    assert result["advanced"]["blocked"] is True
    assert run_phase_get(root=repo_root)["phase"] == "design"


# --- approve/gate digest agreement -------------------------------------------


def test_approval_globs_match_packs() -> None:
    """`_APPROVAL_STORE_NAME_GLOB[phase]` MUST be a subset of that phase's pack
    `approval_recorded: since_name_glob`.

    The map is the live source of truth for what ``run_approve`` digests; the
    pack's ``since_name_glob`` is what the gate predicate re-digests. The map
    may be *narrower* than the pack (e.g. design uses ``"approach.artifact"``
    while the pack says ``"*.artifact"``) so stale pre-split artifacts don't
    shift the digest.  What matters is that both sides re-digest an identical
    row set — the map must not be wider than the pack.

    This caught design: the split narrowed the map from ``"*.artifact"`` to
    ``"approach.artifact"`` so a leftover pre-split ``tasks.artifact`` under
    phase=design can't shift the digest.
    """
    import fnmatch

    from agentalloy.signals.gates import (
        _APPROVAL_STORE_NAME_GLOB,  # pyright: ignore[reportPrivateUsage]
    )
    from agentalloy.signals.skill_loader import exit_gates_for_phase

    def _glob_is_subset(pattern: str, candidate: str) -> bool:
        """Return True when *pattern* matches only files that *candidate* also matches."""
        # Generate a small sample of plausible artifact names and verify every
        # match of *pattern* is also matched by *candidate*.
        sample = [
            "spec.artifact",
            "approach.artifact",
            "tasks.artifact",
            "test-plan.artifact",
            "fast.artifact",
            "anything.artifact",
            "foo.md",
            "bar.artifact.bak",
        ]
        pattern_matches = {n for n in sample if fnmatch.fnmatch(n, pattern)}
        candidate_matches = {n for n in sample if fnmatch.fnmatch(n, candidate)}
        return pattern_matches.issubset(candidate_matches)

    for phase, mapped in _APPROVAL_STORE_NAME_GLOB.items():
        gate = exit_gates_for_phase(phase)
        assert gate is not None, f"{phase}: no exit gate"
        found = [
            leaf["approval_recorded"].get("since_name_glob")
            for leaf in gate.get("all_of", [])
            if isinstance(leaf, dict) and "approval_recorded" in leaf
        ]
        assert found, f"{phase}: pack declares no approval_recorded leaf"
        if not _glob_is_subset(mapped, found[0]):
            pytest.fail(
                f"{phase}: _APPROVAL_STORE_NAME_GLOB {mapped!r} is wider than "
                f"pack since_name_glob {found[0]!r} — approve and the gate would "
                f"digest different sets"
            )


def test_plan_is_approvable(repo_root: Path) -> None:
    """`agentalloy approve plan` is a shipped prose invariant of the plan pack, so
    the phase must actually be approvable."""
    from agentalloy.install.subcommands._state import phase_access

    run_phase_set("plan", root=repo_root, force=True)
    handle = phase_access(repo_root).contracts_handle()
    handle.set_artifact("plan", "x", "tasks.artifact", "# x\n\n## Tasks\n\n- t1\n")
    handle.set_artifact("plan", "x", "test-plan.artifact", "# x\n\n## Test Cases\n\n- AC-1\n")

    result = run_approve("plan", root=repo_root, approver="alice")

    assert result["ok"] is True
    assert phase_access(repo_root).contracts_handle().get_approval("plan") is not None


def test_sdd_fast_is_approvable(repo_root: Path) -> None:
    """`agentalloy approve sdd-fast` must work against the STORE, not disk.

    sdd-fast's approval was disk-backed (`docs/fast/*.md`) long after its exit gate
    became store-backed. The disk glob leaked into `run_approve`'s error text as a
    path, which is how agents learned to hand-write a gitignored `docs/fast/` file
    that the gate could never see. This pins the store branch as the live one:
    an artifact recorded only in the store is sufficient to approve, with no file
    written anywhere.

    `SDD_FAST_REQUIRE_APPROVAL` is off by default, so nothing else exercises this
    path — without this test the branch switch is unverified.
    """
    from agentalloy.install.subcommands._state import phase_access

    run_phase_set("sdd-fast", root=repo_root, force=True)
    handle = phase_access(repo_root).contracts_handle()
    handle.set_artifact(
        "sdd-fast",
        "x",
        "fast.artifact",
        "# x\n\n## Acceptance Criteria\n- a\n\n## Approach\n- b\n\n## Test Cases\n- c\n",
    )

    result = run_approve("sdd-fast", root=repo_root, approver="alice")

    assert result["ok"] is True, result.get("error")
    assert result["marker"] == "state store (approved/sdd-fast)"
    assert phase_access(repo_root).contracts_handle().get_approval("sdd-fast") is not None
    # Nothing was written to the old disk location.
    assert not (repo_root / "docs" / "fast").exists()
