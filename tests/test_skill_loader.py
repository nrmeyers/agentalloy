"""Tests for agentalloy.signals.skill_loader — extracted domain helpers.

The functions in skill_loader are pure-domain (no CLI deps); these tests
exercise them in isolation without going through the signal CLI.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

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
# _load_workflow_skill_for_phase — duckdb path
# ---------------------------------------------------------------------------


def test_load_workflow_skill_reads_from_duckdb(tmp_path: Path) -> None:
    from agentalloy.signals.skill_loader import _load_workflow_skill_for_phase

    db_file = tmp_path / "profile.duck"
    db_file.write_text("")

    exit_gates = {"artifact_exists": {"path": "*.md"}}
    mock_row = (
        "sdd-qa",
        "QA phase prose.",
        ["qa"],
        json.dumps(exit_gates),
        ["done"],
    )

    mock_con = MagicMock()
    mock_con.__enter__ = lambda s: s
    mock_con.__exit__ = MagicMock(return_value=False)
    mock_con.execute.return_value.fetchall.return_value = [mock_row]

    with (
        patch("agentalloy.profiles.detect_profile", return_value=None),
        patch("agentalloy.profiles.profile_datastore_path", return_value=db_file),
        patch("duckdb.connect", return_value=mock_con),
    ):
        result = _load_workflow_skill_for_phase("qa")

    assert result is not None
    assert result["skill_id"] == "sdd-qa"
    assert result["exit_gates"] == exit_gates
    assert result["signal_keywords"] == ["done"]


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
# _write_telemetry (soft-fail — no DB)
# ---------------------------------------------------------------------------


def test_write_telemetry_soft_fails_when_db_missing(tmp_path: Path) -> None:
    """_write_telemetry must not raise even when the DB is absent."""
    from agentalloy.signals.skill_loader import _write_telemetry

    with patch(
        "agentalloy.profiles.domain_datastore_path",
        return_value=tmp_path / "nonexistent.duck",
    ):
        # Should not raise
        _write_telemetry({"phase": "build", "task": "test", "event_type": "phase_eval"})


def test_write_telemetry_soft_fails_on_error() -> None:
    """_write_telemetry catches all exceptions — never propagates."""
    from agentalloy.signals.skill_loader import _write_telemetry

    with patch(
        "agentalloy.profiles.domain_datastore_path",
        side_effect=RuntimeError("broken"),
    ):
        _write_telemetry({"phase": "build"})
