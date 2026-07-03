"""Tests for agentalloy.signals.skill_loader — extracted domain helpers.

The functions in skill_loader are pure-domain (no CLI deps); these tests
exercise them in isolation without going through the signal CLI.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

# ---------------------------------------------------------------------------
# _read_phase
# ---------------------------------------------------------------------------


def test_read_phase_returns_none_when_missing(tmp_path: Path) -> None:
    from agentalloy.signals.skill_loader import _read_phase

    assert _read_phase(tmp_path) is None


def test_read_phase_reads_yaml_dict_format(tmp_path: Path) -> None:
    from agentalloy.signals.skill_loader import _read_phase

    phase_file = tmp_path / ".agentalloy" / "phase"
    phase_file.parent.mkdir(parents=True)
    phase_file.write_text("phase: build\n")

    assert _read_phase(tmp_path) == "build"


def test_read_phase_reads_plain_string_format(tmp_path: Path) -> None:
    from agentalloy.signals.skill_loader import _read_phase

    phase_file = tmp_path / ".agentalloy" / "phase"
    phase_file.parent.mkdir(parents=True)
    phase_file.write_text("spec\n")

    assert _read_phase(tmp_path) == "spec"


def test_read_phase_returns_none_on_malformed_file(tmp_path: Path) -> None:
    from agentalloy.signals.skill_loader import _read_phase

    phase_file = tmp_path / ".agentalloy" / "phase"
    phase_file.parent.mkdir(parents=True)
    # Empty YAML dict value → no "phase" key
    phase_file.write_text("{}  \n")

    # {} is a dict with no "phase" key → None
    assert _read_phase(tmp_path) is None


def test_read_phase_strips_whitespace(tmp_path: Path) -> None:
    from agentalloy.signals.skill_loader import _read_phase

    phase_file = tmp_path / ".agentalloy" / "phase"
    phase_file.parent.mkdir(parents=True)
    phase_file.write_text("phase:  qa  \n")

    assert _read_phase(tmp_path) == "qa"


# ---------------------------------------------------------------------------
# _read_lifecycle_mode / _write_lifecycle_mode
# ---------------------------------------------------------------------------


def test_lifecycle_mode_defaults_to_full_when_absent(tmp_path: Path) -> None:
    from agentalloy.signals.skill_loader import _read_lifecycle_mode

    # No .agentalloy/config at all -> historical behavior must be preserved.
    assert _read_lifecycle_mode(tmp_path) == "full"


def test_lifecycle_mode_round_trips_each_mode(tmp_path: Path) -> None:
    from agentalloy.signals.skill_loader import (
        LIFECYCLE_MODES,
        _read_lifecycle_mode,
        _write_lifecycle_mode,
    )

    # Two-mode world after the hook transport was removed: full / off.
    assert LIFECYCLE_MODES == ("full", "off")
    for mode in LIFECYCLE_MODES:
        _write_lifecycle_mode(tmp_path, mode)
        assert (tmp_path / ".agentalloy" / "config").read_text() == f"lifecycle_mode: {mode}\n"
        assert _read_lifecycle_mode(tmp_path) == mode


def test_lifecycle_mode_legacy_assist_reads_as_off(tmp_path: Path) -> None:
    """Legacy ``assist`` collapsed to ``off`` when the hook transport was removed.

    It must NOT fall through to the ``full`` default (which would wrongly
    re-enable composition for repos that had opted into assist).
    """
    from agentalloy.signals.skill_loader import _read_lifecycle_mode

    config = tmp_path / ".agentalloy" / "config"
    config.parent.mkdir(parents=True)
    config.write_text("lifecycle_mode: assist\n")

    assert _read_lifecycle_mode(tmp_path) == "off"


def test_lifecycle_mode_unknown_value_falls_back_to_full(tmp_path: Path) -> None:
    from agentalloy.signals.skill_loader import _read_lifecycle_mode

    config = tmp_path / ".agentalloy" / "config"
    config.parent.mkdir(parents=True)
    config.write_text("lifecycle_mode: bananas\n")  # not a valid mode

    # An unrecognized value must never silently disable the lifecycle.
    assert _read_lifecycle_mode(tmp_path) == "full"


def test_lifecycle_mode_malformed_file_falls_back_to_full(tmp_path: Path) -> None:
    from agentalloy.signals.skill_loader import _read_lifecycle_mode

    config = tmp_path / ".agentalloy" / "config"
    config.parent.mkdir(parents=True)
    config.write_text(": : not yaml : :\n[broken")

    assert _read_lifecycle_mode(tmp_path) == "full"


def test_write_lifecycle_mode_rejects_invalid_mode(tmp_path: Path) -> None:
    from agentalloy.signals.skill_loader import _write_lifecycle_mode

    with pytest.raises(ValueError, match="invalid lifecycle mode"):
        _write_lifecycle_mode(tmp_path, "turbo")


# ---------------------------------------------------------------------------
# _write_phase_atomic
# ---------------------------------------------------------------------------


def test_write_phase_atomic_creates_file(tmp_path: Path) -> None:
    from agentalloy.signals.skill_loader import _write_phase_atomic

    _write_phase_atomic(tmp_path, "design")
    phase_file = tmp_path / ".agentalloy" / "phase"
    assert phase_file.exists()
    content = yaml.safe_load(phase_file.read_text())
    assert content["phase"] == "design"


def test_write_phase_atomic_overwrites_existing(tmp_path: Path) -> None:
    from agentalloy.signals.skill_loader import _write_phase_atomic

    _write_phase_atomic(tmp_path, "spec")
    _write_phase_atomic(tmp_path, "design")
    phase_file = tmp_path / ".agentalloy" / "phase"
    content = yaml.safe_load(phase_file.read_text())
    assert content["phase"] == "design"


def test_write_phase_atomic_creates_parent_dirs(tmp_path: Path) -> None:
    from agentalloy.signals.skill_loader import _write_phase_atomic

    nested = tmp_path / "project"
    _write_phase_atomic(nested, "build")
    assert (nested / ".agentalloy" / "phase").exists()


def test_write_phase_atomic_no_tmp_file_left(tmp_path: Path) -> None:
    from agentalloy.signals.skill_loader import _write_phase_atomic

    _write_phase_atomic(tmp_path, "build")
    tmp = tmp_path / ".agentalloy" / "phase.tmp"
    assert not tmp.exists()


# ---------------------------------------------------------------------------
# _load_workflow_skill_for_phase — packs fallback
# ---------------------------------------------------------------------------


def test_load_workflow_skill_for_phase_falls_back_to_packs(tmp_path: Path) -> None:
    """When DB access raises an exception, fall through to _load_workflow_skill_from_packs."""
    from agentalloy.signals.skill_loader import _load_workflow_skill_for_phase

    skill_data: dict[str, Any] = {
        "skill_id": "sdd-build-packs",
        "skill_class": "workflow",
        "raw_prose": "Build phase instructions.",
        "applies_to_phases": ["build"],
        "exit_gates": {},
        "signal_keywords": ["done", "ready"],
    }

    with (
        patch("agentalloy.profiles.detect_profile", side_effect=RuntimeError("db broken")),
        patch(
            "agentalloy.signals.skill_loader._load_workflow_skill_from_packs",
            return_value=skill_data,
        ) as mock_packs,
    ):
        result = _load_workflow_skill_for_phase("build")
        mock_packs.assert_called_once_with("build")

    assert result is not None
    assert result["skill_id"] == "sdd-build-packs"


def test_load_workflow_skill_returns_none_for_unknown_phase() -> None:
    from agentalloy.signals.skill_loader import _load_workflow_skill_for_phase

    with (
        patch("agentalloy.profiles.detect_profile", return_value=None),
        patch(
            "agentalloy.profiles.profile_datastore_path",
            return_value=Path("/nonexistent/db.duck"),
        ),
        patch(
            "agentalloy.signals.skill_loader._load_workflow_skill_from_packs",
            return_value=None,
        ),
    ):
        result = _load_workflow_skill_for_phase("nonexistent_phase")

    assert result is None


# ---------------------------------------------------------------------------
# _load_workflow_skill_for_phase — shipped-first lock + invariant guard
# ---------------------------------------------------------------------------


def test_workflow_override_supplies_only_prose(tmp_path: Path) -> None:
    """A profile override contributes raw_prose (+domain_tags); the load-bearing
    structured fields (exit_gates etc.) are re-sourced from the shipped skill."""
    from agentalloy.signals import skill_loader
    from agentalloy.signals.invariants import derive_invariants

    shipped = skill_loader._load_workflow_skill_from_packs("design")
    assert shipped is not None
    # Reworded prose that still contains every load-bearing invariant token.
    reworded = "REWORDED design guidance. Keeps: " + " ".join(derive_invariants(shipped))

    with patch.object(
        skill_loader, "_load_workflow_prose_override", return_value=(reworded, ["t"])
    ):
        result = skill_loader._load_workflow_skill_for_phase("design")

    assert result is not None
    assert result["raw_prose"] == reworded  # override prose applied
    assert result["exit_gates"] == shipped["exit_gates"]  # locked: from shipped
    assert result["domain_tags"] == ["t"]


def test_workflow_override_missing_invariant_falls_back_to_shipped(tmp_path: Path) -> None:
    from agentalloy.signals import skill_loader
    from agentalloy.signals.invariants import derive_invariants

    shipped = skill_loader._load_workflow_skill_from_packs("design")
    assert shipped is not None
    assert derive_invariants(shipped)  # design has load-bearing tokens to drop

    bad = "REWORDED but drops every load-bearing path and command."
    with patch.object(skill_loader, "_load_workflow_prose_override", return_value=(bad, None)):
        result = skill_loader._load_workflow_skill_for_phase("design")

    assert result is not None
    assert result["raw_prose"] == shipped["raw_prose"]  # shipped prose served
    assert result["exit_gates"] == shipped["exit_gates"]


def test_workflow_no_override_returns_shipped(tmp_path: Path) -> None:
    from agentalloy.signals import skill_loader

    shipped = skill_loader._load_workflow_skill_from_packs("design")
    assert shipped is not None
    with patch.object(skill_loader, "_load_workflow_prose_override", return_value=(None, None)):
        result = skill_loader._load_workflow_skill_for_phase("design")

    assert result is not None
    assert result["raw_prose"] == shipped["raw_prose"]
    assert result["exit_gates"] == shipped["exit_gates"]


# ---------------------------------------------------------------------------
# _build_predicate_context
# ---------------------------------------------------------------------------


def test_build_predicate_context_basic(tmp_path: Path) -> None:
    from agentalloy.signals.predicates import PredicateContext
    from agentalloy.signals.skill_loader import _build_predicate_context

    ctx = _build_predicate_context(tmp_path, phase="build", prompt_text="hello")
    assert isinstance(ctx, PredicateContext)
    assert ctx.project_root == tmp_path
    assert ctx.current_phase == "build"
    assert ctx.recent_prompt_text == "hello"
    assert ctx.recent_tool_use is None
    assert ctx.contracts_root == tmp_path / ".agentalloy" / "contracts"


def test_build_predicate_context_with_tool(tmp_path: Path) -> None:
    from agentalloy.signals.skill_loader import _build_predicate_context

    ctx = _build_predicate_context(
        tmp_path,
        phase="spec",
        tool_name="git commit",
        tool_path="/repo",
    )
    assert ctx.recent_tool_use == {"tool": "git commit", "path": "/repo", "args": {}}


def test_build_predicate_context_no_tool(tmp_path: Path) -> None:
    from agentalloy.signals.skill_loader import _build_predicate_context

    ctx = _build_predicate_context(tmp_path, phase="design")
    assert ctx.recent_tool_use is None


def test_build_predicate_context_no_phase(tmp_path: Path) -> None:
    from agentalloy.signals.skill_loader import _build_predicate_context

    ctx = _build_predicate_context(tmp_path, phase=None)
    assert ctx.current_phase is None


def test_build_predicate_context_file_events(tmp_path: Path) -> None:
    from agentalloy.signals.skill_loader import _build_predicate_context

    events = [tmp_path / "a.py", tmp_path / "b.py"]
    ctx = _build_predicate_context(tmp_path, phase="build", file_events=events)
    assert ctx.file_events_since == events


def test_build_predicate_context_empty_file_events(tmp_path: Path) -> None:
    from agentalloy.signals.skill_loader import _build_predicate_context

    ctx = _build_predicate_context(tmp_path, phase="build")
    assert ctx.file_events_since == []


# ---------------------------------------------------------------------------
# Runtime-state relocation (AGENTALLOY_RUNTIME_STATE_DIR)
# ---------------------------------------------------------------------------


class TestRuntimeStateRelocation:
    """Proxy-exclusive cadence keys relocate out of the repo when
    AGENTALLOY_RUNTIME_STATE_DIR is set; ``cursor`` (host-CLI shared) never
    moves. Per-turn writes inside the repo trip harness file-watchers."""

    def test_relocated_key_writes_outside_repo(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agentalloy.api.proxy_context import encode_proj_token
        from agentalloy.signals.skill_loader import _read_announced, _write_announced_atomic

        repo = tmp_path / "repo"
        repo.mkdir()
        state_root = tmp_path / "runtime-state"
        monkeypatch.setenv("AGENTALLOY_RUNTIME_STATE_DIR", str(state_root))

        _write_announced_atomic(repo, "intake", ["s1"])

        assert not (repo / ".agentalloy" / "announced").exists()
        relocated = state_root / encode_proj_token(repo) / "announced"
        assert relocated.exists()
        assert _read_announced(repo) == "intake"

    def test_cursor_stays_in_repo(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from agentalloy.signals.skill_loader import _read_cursor, _write_cursor_atomic

        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.setenv("AGENTALLOY_RUNTIME_STATE_DIR", str(tmp_path / "runtime-state"))

        _write_cursor_atomic(repo, "build/thing.md")

        assert (repo / ".agentalloy" / "cursor").exists()
        assert _read_cursor(repo) == "build/thing.md"

    def test_legacy_in_repo_value_read_then_cleaned_on_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agentalloy.signals.skill_loader import _read_announced, _write_announced_atomic

        repo = tmp_path / "repo"
        (repo / ".agentalloy").mkdir(parents=True)
        (repo / ".agentalloy" / "announced").write_text("spec\n")
        monkeypatch.setenv("AGENTALLOY_RUNTIME_STATE_DIR", str(tmp_path / "runtime-state"))

        # Pre-relocation cadence survives the move...
        assert _read_announced(repo) == "spec"
        # ...and the next write migrates it out and removes the repo copy.
        _write_announced_atomic(repo, "build")
        assert not (repo / ".agentalloy" / "announced").exists()
        assert _read_announced(repo) == "build"

    def test_unset_env_keeps_repo_local_behavior(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agentalloy.signals.skill_loader import _read_announced, _write_announced_atomic

        monkeypatch.delenv("AGENTALLOY_RUNTIME_STATE_DIR", raising=False)
        repo = tmp_path / "repo"
        repo.mkdir()

        _write_announced_atomic(repo, "intake")

        assert (repo / ".agentalloy" / "announced").exists()
        assert _read_announced(repo) == "intake"

    def test_clear_state_removes_both_locations(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agentalloy.signals.skill_loader import (
            _clear_state,
            _read_state,
            _write_state_atomic,
        )

        repo = tmp_path / "repo"
        (repo / ".agentalloy").mkdir(parents=True)
        (repo / ".agentalloy" / "composed").write_text("old\n")
        monkeypatch.setenv("AGENTALLOY_RUNTIME_STATE_DIR", str(tmp_path / "runtime-state"))
        _write_state_atomic(repo, "composed", "new")
        # Recreate a stray legacy copy, then clear must remove both.
        (repo / ".agentalloy" / "composed").write_text("stale\n")

        _clear_state(repo, "composed")

        assert _read_state(repo, "composed") is None
        assert not (repo / ".agentalloy" / "composed").exists()
