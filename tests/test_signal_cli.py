"""CLI smoke tests for agentalloy signal subcommands."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_phase(project_root: Path, phase: str) -> None:
    pf = project_root / ".agentalloy" / "phase"
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text(f"phase: {phase}\n")


def _write_minimal_skill(packs_root: Path, phase: str = "build") -> None:
    """Write a minimal workflow skill yaml for the given phase into a tmp _packs dir."""
    packs_root.mkdir(parents=True, exist_ok=True)
    data = {
        "skill_id": f"sdd-{phase}",
        "skill_class": "workflow",
        "canonical_name": f"sdd-{phase}",
        "raw_prose": f"Workflow skill for {phase} phase. " * 5,
        "applies_to_phases": [phase],
        "exit_gates": {"artifact_exists": {"path": "*.md"}},
        "signal_keywords": [phase, "done", "ready"],
        "contract_template": "---\nphase: build\n---\n\nbody\n",
    }
    (packs_root / f"sdd-{phase}.yaml").write_text(yaml.dump(data))


# ---------------------------------------------------------------------------
# evaluate-phase — no prefilter match → exit 0 with matched: false
# ---------------------------------------------------------------------------


def test_evaluate_phase_no_prefilter_match_exit_0(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from agentalloy.install.subcommands import signal as sig

    _write_phase(tmp_path, "build")
    monkeypatch.chdir(tmp_path)

    # Patch skill loader to return a skill with no matching signal_keywords
    skill = {
        "skill_id": "sdd-build",
        "raw_prose": "prose",
        "applies_to_phases": ["build"],
        "exit_gates": {"artifact_exists": {"path": "nope.md"}},
        "signal_keywords": ["unusedkeyword"],
    }
    with (
        patch.object(sig, "_load_workflow_skill_for_phase", return_value=skill),
        patch.object(sig, "_write_telemetry"),
    ):
        args = argparse.Namespace(
            prompt_file=None,
            tool=None,
            tool_path=None,
        )
        import io
        import sys

        captured = io.StringIO()
        sys.stdout = captured
        try:
            rc = sig._evaluate_phase(args)  # pyright: ignore[reportPrivateUsage]
        finally:
            sys.stdout = sys.__stdout__

    assert rc == 0
    out = json.loads(captured.getvalue())
    assert out.get("matched") is False


# ---------------------------------------------------------------------------
# evaluate-phase — transition writes workflow skill to stdout
# ---------------------------------------------------------------------------


def test_evaluate_phase_transition_writes_workflow_skill_to_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from agentalloy.install.subcommands import signal as sig

    _write_phase(tmp_path, "spec")
    monkeypatch.chdir(tmp_path)

    # Create the artifact that the gate checks for
    (tmp_path / "spec.md").write_text("x" * 900)

    skill = {
        "skill_id": "sdd-spec",
        "raw_prose": "THE DESIGN PROSE",
        "applies_to_phases": ["spec"],
        "exit_gates": {"artifact_exists": {"path": "spec.md"}},
        "signal_keywords": ["done", "ready"],
    }
    next_skill: dict[str, Any] = {
        "skill_id": "sdd-design",
        "raw_prose": "DESIGN WORKFLOW PROSE",
        "applies_to_phases": ["design"],
        "exit_gates": {},
        "signal_keywords": [],
    }

    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("done, ready to move on")

    def mock_load(phase: str, *args: Any) -> dict[str, Any]:
        return skill if phase == "spec" else next_skill

    import io
    import sys

    captured_stdout = io.StringIO()
    with (
        patch.object(sig, "_load_workflow_skill_for_phase", side_effect=mock_load),
        patch.object(sig, "_write_telemetry"),
    ):
        args = argparse.Namespace(
            prompt_file=str(prompt_file),
            tool=None,
            tool_path=None,
        )
        sys.stdout = captured_stdout
        sys.stderr = io.StringIO()
        try:
            rc = sig._evaluate_phase(args)  # pyright: ignore[reportPrivateUsage]
        finally:
            sys.stdout = sys.__stdout__
            sys.stderr = sys.__stderr__

    assert rc == 0
    output = captured_stdout.getvalue()
    assert "DESIGN WORKFLOW PROSE" in output
    assert "[agentalloy-workflow]" in output

    # Phase file should be updated to "design"
    phase_file = tmp_path / ".agentalloy" / "phase"
    content = yaml.safe_load(phase_file.read_text())
    assert content["phase"] == "design"


# ---------------------------------------------------------------------------
# evaluate-system — emits matching skill bodies
# ---------------------------------------------------------------------------


def test_evaluate_system_emits_matching_skill_bodies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from agentalloy.install.subcommands import signal as sig

    monkeypatch.chdir(tmp_path)

    import io
    import sys

    captured = io.StringIO()

    # Patch the DuckDB call to simulate a system skill
    with (
        patch("agentalloy.install.subcommands.signal._read_phase", return_value="build"),
        patch("agentalloy.install.subcommands.signal._write_telemetry"),
        patch("duckdb.connect") as mock_conn,
    ):
        # system skill applies_when: tool_use_about_to_fire for git commit
        applies_when = yaml.dump(
            {"all_of": [{"tool_use_about_to_fire": {"tools": ["git commit"]}}]}
        )
        mock_con = MagicMock()
        mock_con.__enter__ = lambda s: s  # pyright: ignore[reportUnknownLambdaType]
        mock_con.__exit__ = MagicMock(return_value=False)
        mock_con.execute.return_value.fetchall.return_value = [
            ("commit-safety", "COMMIT SAFETY PROSE", applies_when)
        ]
        mock_conn.return_value = mock_con

        # Patch datastore path to exist
        db_file = tmp_path / "skills.duck"
        db_file.write_text("")

        with (
            patch("agentalloy.profiles.profile_datastore_path", return_value=db_file),
            patch("agentalloy.profiles.detect_profile", return_value=None),
        ):
            args = argparse.Namespace(tool="git commit")
            sys.stdout = captured
            try:
                rc = sig._evaluate_system(args)  # pyright: ignore[reportPrivateUsage]
            finally:
                sys.stdout = sys.__stdout__

    assert rc == 0
    output = captured.getvalue()
    assert "COMMIT SAFETY PROSE" in output
    assert "[agentalloy-system:commit-safety]" in output


# ---------------------------------------------------------------------------
# watch-contract — validates and invokes compose
# ---------------------------------------------------------------------------


def test_watch_contract_invokes_compose(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import yaml

    from agentalloy.install.subcommands import signal as sig

    monkeypatch.chdir(tmp_path)

    contract_path = tmp_path / ".agentalloy" / "contracts" / "build" / "task.md"
    contract_path.parent.mkdir(parents=True, exist_ok=True)
    fm: dict[str, Any] = {
        "phase": "build",
        "task_slug": "test-task",
        "domain_tags": ["NestJS"],
        "scope": {"touches": [], "avoids": []},
        "success_criteria": [],
        "related_contracts": [],
    }
    contract_path.write_text(f"---\n{yaml.dump(fm)}---\n\nTask body.\n")

    _write_phase(tmp_path, "build")

    with (
        patch("subprocess.run") as mock_run,
        patch("agentalloy.install.subcommands.signal._write_telemetry"),
        patch("agentalloy.install.state.load_state", return_value={"port": 47950}),
    ):
        mock_run.return_value = MagicMock(returncode=0)
        args = argparse.Namespace(path=str(contract_path))
        rc = sig._watch_contract(args)  # pyright: ignore[reportPrivateUsage]

    assert rc == 0
    mock_run.assert_called_once()
    call_args = mock_run.call_args[0][0]
    assert "compose" in call_args
    assert "--contract" in call_args


# ---------------------------------------------------------------------------
# check — returns structured JSON
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# evaluate-phase — advisory emitted to stdout when artifact_completeness gate present
# ---------------------------------------------------------------------------


def test_evaluate_phase_emits_advisory_to_stdout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Advisory text from artifact_completeness appears in stdout alongside the transition output."""
    from agentalloy.install.subcommands import signal as sig

    _write_phase(tmp_path, "spec")
    monkeypatch.chdir(tmp_path)

    spec_file = tmp_path / "spec.md"
    spec_file.write_text("# Spec\nsome content\n")

    skill = {
        "skill_id": "sdd-spec",
        "raw_prose": "SPEC PROSE",
        "applies_to_phases": ["spec"],
        "exit_gates": {
            "all_of": [
                {"artifact_exists": {"path": "spec.md"}},
                {"artifact_completeness": {"path": "spec.md", "criteria": "ACs testable"}},
            ]
        },
        "signal_keywords": ["done"],
    }
    next_skill: dict[str, Any] = {
        "skill_id": "sdd-design",
        "raw_prose": "DESIGN PROSE",
        "applies_to_phases": ["design"],
        "exit_gates": {},
        "signal_keywords": [],
    }

    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("done")

    def mock_load(phase: str, *args: Any) -> dict[str, Any]:
        return skill if phase == "spec" else next_skill

    import io
    import sys

    captured_stdout = io.StringIO()
    with (
        patch.object(sig, "_load_workflow_skill_for_phase", side_effect=mock_load),
        patch.object(sig, "_write_telemetry"),
        patch(
            "agentalloy.install.subcommands.signal.get_embed_client",
            side_effect=RuntimeError("no server"),
        ),
    ):
        args = argparse.Namespace(prompt_file=str(prompt_file), tool=None, tool_path=None)
        sys.stdout = captured_stdout
        sys.stderr = io.StringIO()
        try:
            rc = sig._evaluate_phase(args)  # pyright: ignore[reportPrivateUsage]
        finally:
            sys.stdout = sys.__stdout__
            sys.stderr = sys.__stderr__

    assert rc == 0
    output = captured_stdout.getvalue()
    # artifact_exists MET → all_of short-circuits at UNKNOWN (from artifact_completeness) → no transition
    # but advisory should still appear
    assert "[agentalloy-eval]" in output
    assert "ACs testable" in output


def test_evaluate_phase_lm_client_constructed_from_embed_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """_evaluate_phase builds an embed client against runtime_embed_base_url."""
    from agentalloy.install.subcommands import signal as sig

    _write_phase(tmp_path, "build")
    monkeypatch.chdir(tmp_path)

    skill = {
        "skill_id": "sdd-build",
        "raw_prose": "prose",
        "applies_to_phases": ["build"],
        "exit_gates": {"artifact_exists": {"path": "nope.md"}},
        "signal_keywords": ["AGENTALLOY_FORCE_CHECK"],
    }

    import io
    import sys

    constructed_urls: list[str] = []

    class _FakeClient:  # pyright: ignore[reportUnknownParameterType, reportMissingParameterType, reportUnknownMemberType, reportUnknownArgumentType]
        def __init__(self, settings: Any) -> None:  # pyright: ignore[reportUnknownParameterType, reportMissingParameterType]
            constructed_urls.append(settings.runtime_embed_base_url)

    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("done")

    with (
        patch.object(sig, "_load_workflow_skill_for_phase", return_value=skill),
        patch.object(sig, "_write_telemetry"),
        patch.object(sig, "get_embed_client", _FakeClient),
        patch.dict(os.environ, {"AGENTALLOY_FORCE_CHECK": "1"}),
    ):
        args = argparse.Namespace(prompt_file=str(prompt_file), tool=None, tool_path=None)
        sys.stdout = io.StringIO()
        sys.stderr = io.StringIO()
        try:
            sig._evaluate_phase(args)  # pyright: ignore[reportPrivateUsage]
        finally:
            sys.stdout = sys.__stdout__
            sys.stderr = sys.__stderr__

    assert len(constructed_urls) == 1
    assert "47951" in constructed_urls[0] or "localhost" in constructed_urls[0]


# ---------------------------------------------------------------------------
# check — returns structured JSON
# ---------------------------------------------------------------------------


def test_check_returns_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from agentalloy.install.subcommands import signal as sig

    monkeypatch.chdir(tmp_path)
    _write_phase(tmp_path, "build")

    import io
    import sys

    captured = io.StringIO()
    with patch.object(sig, "_load_workflow_skill_for_phase", return_value=None):
        args = argparse.Namespace(json_out=True)
        sys.stdout = captured
        try:
            rc = sig._check(args)  # pyright: ignore[reportPrivateUsage]
        finally:
            sys.stdout = sys.__stdout__

    assert rc == 0
    data = json.loads(captured.getvalue())
    assert data["current_phase"] == "build"


# ---------------------------------------------------------------------------
# Hook event routing test (simulates agentalloy-signal.sh logic)
# ---------------------------------------------------------------------------


def test_hook_routing_ups_calls_evaluate_phase(tmp_path: Path):
    """AGENTALLOY_HOOK_EVENT=UserPromptSubmit routes to evaluate-phase."""
    _write_phase(tmp_path, "build")

    with (
        patch("agentalloy.install.subcommands.signal._evaluate_phase", return_value=0) as mock_ep,
        patch(
            "agentalloy.install.subcommands.signal._load_workflow_skill_for_phase",
            return_value=None,
        ),
    ):
        import argparse

        from agentalloy.install.subcommands.signal import (
            _dispatch,  # pyright: ignore[reportPrivateUsage]
        )

        args = argparse.Namespace(
            signal_cmd="evaluate-phase", prompt_file=None, tool=None, tool_path=None
        )
        rc = _dispatch(args)

    assert rc == 0
    mock_ep.assert_called_once()


def test_hook_routing_pretool_calls_evaluate_system(tmp_path: Path):
    """signal_cmd=evaluate-system routes to _evaluate_system."""
    import argparse

    from agentalloy.install.subcommands.signal import (
        _dispatch,  # pyright: ignore[reportPrivateUsage]
    )

    with patch("agentalloy.install.subcommands.signal._evaluate_system", return_value=0) as mock_es:
        args = argparse.Namespace(signal_cmd="evaluate-system", tool="Bash")
        rc = _dispatch(args)

    assert rc == 0
    mock_es.assert_called_once()


# ---------------------------------------------------------------------------
# _guard_system_prose — runtime invariant fall-back for system overrides
# ---------------------------------------------------------------------------


def test_guard_system_prose_keeps_override_when_invariants_present() -> None:
    from agentalloy.install.subcommands.signal import _guard_system_prose

    shipped = {
        "skill_id": "sys-x",
        "raw_prose": "SHIPPED keeps foo.md",
        "exit_gates": {"all_of": [{"artifact_exists": {"path": "docs/**/foo.md"}}]},
    }
    with patch("agentalloy.signals.invariants.load_shipped_skill", return_value=shipped):
        assert _guard_system_prose("sys-x", "OVERRIDE keeps foo.md") == "OVERRIDE keeps foo.md"


def test_guard_system_prose_falls_back_when_invariant_dropped() -> None:
    from agentalloy.install.subcommands.signal import _guard_system_prose

    shipped = {
        "skill_id": "sys-x",
        "raw_prose": "SHIPPED keeps foo.md",
        "exit_gates": {"all_of": [{"artifact_exists": {"path": "docs/**/foo.md"}}]},
    }
    with patch("agentalloy.signals.invariants.load_shipped_skill", return_value=shipped):
        assert _guard_system_prose("sys-x", "OVERRIDE without the token") == "SHIPPED keeps foo.md"


def test_guard_system_prose_noop_when_no_shipped_default() -> None:
    from agentalloy.install.subcommands.signal import _guard_system_prose

    with patch("agentalloy.signals.invariants.load_shipped_skill", return_value=None):
        assert _guard_system_prose("orphan", "OVERRIDE prose") == "OVERRIDE prose"
