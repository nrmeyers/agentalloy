"""TD1-TD9 for slice 12-harness-enforcement-posture.

Covers the posture *content* — which phases deny, what they deny, what stays
writable, and the WireRecord guard. The plumbing that calls these (the
phase-advance seam) is covered in tests/api/test_state_router.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agentalloy.api import state_client as state_client_mod
from agentalloy.install.subcommands import flow as flow_mod
from agentalloy.install.subcommands import phase as phase_mod
from agentalloy.install.subcommands import wire_harness
from agentalloy.providers.base import (
    DENIED_PHASES,
    build_claude_code_permissions,
    build_codex_workspace_write,
    build_denial_message,
    is_tier_a_enforced,
)

PRE_BUILD = ("intake", "spec", "design")
UNLOCKED = ("build", "qa", "ship", "sdd-fast", "add-skill")


# ---------------------------------------------------------------------------
# TD1, TD2 — deny rules present at intake, spec, design
# ---------------------------------------------------------------------------


class TestTD1TD2DenyRulesPreBuild:
    @pytest.mark.parametrize("phase", PRE_BUILD)
    def test_src_and_tests_denied(self, phase: str) -> None:
        deny = build_claude_code_permissions(phase)["deny"]
        assert isinstance(deny, list)
        assert set(deny) == {
            "Write(src/**)",
            "Edit(src/**)",
            "Write(tests/**)",
            "Edit(tests/**)",
        }

    def test_denied_phase_set_is_an_explicit_allowlist(self) -> None:
        """Not 'deny unless build' — qa/ship must not be swept in."""
        assert frozenset(PRE_BUILD) == DENIED_PHASES


# ---------------------------------------------------------------------------
# TD3 — denial names phase and owed artifact
# ---------------------------------------------------------------------------


class TestTD3DenialMessage:
    @pytest.mark.parametrize(
        ("phase", "fragment"),
        [
            ("intake", "a contract"),
            ("spec", "docs/spec/<slug>.md"),
            ("design", "docs/design/<slug>/{approach,tasks,test-plan}.md"),
        ],
    )
    def test_names_phase_and_artifact(self, phase: str, fragment: str) -> None:
        msg = build_denial_message(phase)
        assert f"`{phase}`" in msg
        assert fragment in msg

    def test_explicit_owed_artifacts_override(self) -> None:
        msg = build_denial_message("spec", ["docs/spec/widget.md"])
        assert "docs/spec/widget.md" in msg
        assert "<slug>" not in msg

    def test_unknown_phase_falls_back(self) -> None:
        assert "the phase's deliverable" in build_denial_message("nonsense")


# ---------------------------------------------------------------------------
# TD4 — docs/** writable in all pre-build phases
# TD5 — no Bash deny rule
# ---------------------------------------------------------------------------


class TestTD4TD5NonGoals:
    @pytest.mark.parametrize("phase", PRE_BUILD)
    def test_docs_never_denied(self, phase: str) -> None:
        deny = build_claude_code_permissions(phase)["deny"]
        assert isinstance(deny, list)
        assert not any("docs" in rule for rule in deny)

    @pytest.mark.parametrize("phase", PRE_BUILD)
    def test_docs_stays_a_codex_writable_root(self, phase: str) -> None:
        roots = build_codex_workspace_write(phase)["writable_roots"]
        assert isinstance(roots, list)
        assert "docs/" in roots

    @pytest.mark.parametrize("phase", PRE_BUILD)
    def test_no_bash_deny_rule(self, phase: str) -> None:
        """A shell defeats deny rules; this is phase discipline, not a sandbox."""
        deny = build_claude_code_permissions(phase)["deny"]
        assert isinstance(deny, list)
        assert not any("Bash" in rule for rule in deny)


# ---------------------------------------------------------------------------
# TD6, TD7 — build unlocks; codex widens and narrows with phase
# ---------------------------------------------------------------------------


class TestTD6TD7Unlock:
    @pytest.mark.parametrize("phase", UNLOCKED)
    def test_claude_code_unlocked(self, phase: str) -> None:
        assert build_claude_code_permissions(phase) == {"deny": []}

    @pytest.mark.parametrize("phase", UNLOCKED)
    def test_codex_unrestricted(self, phase: str) -> None:
        assert build_codex_workspace_write(phase) == {}

    def test_qa_is_not_denied(self) -> None:
        """QA legitimately edits src/ and tests/ to fix what it finds."""
        assert build_claude_code_permissions("qa")["deny"] == []

    def test_unknown_phase_fails_open(self) -> None:
        assert build_claude_code_permissions("")["deny"] == []
        assert build_codex_workspace_write("garbage") == {}

    def test_codex_narrows_then_widens(self) -> None:
        assert build_codex_workspace_write("design")["writable_roots"] == [
            "docs/",
            ".agentalloy/",
        ]
        assert build_codex_workspace_write("build") == {}


# ---------------------------------------------------------------------------
# TD8 — no WireRecord, no file written
# ---------------------------------------------------------------------------


def _wire_state(root: Path, harnesses: list[str]) -> dict[str, Any]:
    return {"harness_files_written": [{"repo_root": str(root), "harness": h} for h in harnesses]}


class TestTD8WireRecordGuard:
    def test_unwired_repo_untouched(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = tmp_path / ".claude" / "settings.local.json"
        settings.parent.mkdir(parents=True)
        settings.write_text('{"env": {"KEEP": "1"}}\n')
        monkeypatch.setattr(wire_harness.install_state, "load_state", lambda _root: {})

        assert wire_harness.rewrite_enforcement_posture(tmp_path, "design") == []
        assert json.loads(settings.read_text()) == {"env": {"KEEP": "1"}}

    def test_wired_repo_rewritten_and_other_keys_preserved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = tmp_path / ".claude" / "settings.local.json"
        settings.parent.mkdir(parents=True)
        settings.write_text('{"env": {"KEEP": "1"}}\n')
        monkeypatch.setattr(
            wire_harness.install_state,
            "load_state",
            lambda _root: _wire_state(tmp_path, ["claude-code"]),
        )

        assert wire_harness.rewrite_enforcement_posture(tmp_path, "design") == ["claude-code"]
        data = json.loads(settings.read_text())
        assert data["env"] == {"KEEP": "1"}
        assert "Write(src/**)" in data["permissions"]["deny"]

    def test_missing_settings_file_is_not_created(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            wire_harness.install_state,
            "load_state",
            lambda _root: _wire_state(tmp_path, ["claude-code"]),
        )

        assert wire_harness.rewrite_enforcement_posture(tmp_path, "design") == []
        assert not (tmp_path / ".claude" / "settings.local.json").exists()


# ---------------------------------------------------------------------------
# TD9 — banner absent under Tier A enforcement, present on Tier B/C
# ---------------------------------------------------------------------------


class TestTD9Banner:
    def test_banner_shown_when_unwired(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(wire_harness.install_state, "load_state", lambda _root: {})
        assert wire_harness.should_show_banner(tmp_path) is True

    @pytest.mark.parametrize("harness", ["claude-code", "codex"])
    def test_banner_dropped_under_tier_a(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: str
    ) -> None:
        monkeypatch.setattr(
            wire_harness.install_state,
            "load_state",
            lambda _root: _wire_state(tmp_path, [harness]),
        )
        assert wire_harness.should_show_banner(tmp_path) is False

    def test_banner_kept_for_tier_b_harness(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A wired-but-not-Tier-A repo has no enforcement, so it keeps the banner."""
        monkeypatch.setattr(
            wire_harness.install_state,
            "load_state",
            lambda _root: _wire_state(tmp_path, ["cursor"]),
        )
        assert wire_harness.should_show_banner(tmp_path) is True

    def test_tier_a_membership(self) -> None:
        assert is_tier_a_enforced("claude-code")
        assert is_tier_a_enforced("codex")
        assert not is_tier_a_enforced("cursor")


# ---------------------------------------------------------------------------
# CLI call sites must forward repo_root — without it the service skips the
# posture rewrite entirely and the whole slice is dead on the CLI path.
# ---------------------------------------------------------------------------


class TestCLIForwardsRepoRoot:
    @pytest.fixture
    def captured(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        seen: dict[str, Any] = {}

        def _set_phase(_self: Any, value: str, *, repo_root: str | None = None) -> dict[str, Any]:
            seen["value"] = value
            seen["repo_root"] = repo_root
            return {"kind": "phase", "value": value}

        monkeypatch.setattr(state_client_mod.StateClient, "is_running", lambda _self: True)
        monkeypatch.setattr(state_client_mod.StateClient, "set_phase", _set_phase)
        return seen

    def test_phase_set_forwards_repo_root(self, captured: dict[str, Any], tmp_path: Path) -> None:
        phase_mod.run_phase_set("build", root=tmp_path)
        assert captured["repo_root"] == str(tmp_path)

    def test_flow_free_forwards_repo_root(self, captured: dict[str, Any], tmp_path: Path) -> None:
        flow_mod.run_flow_free(root=tmp_path)
        assert captured["repo_root"] == str(tmp_path)
        assert captured["value"].startswith("free-flow:")

    def test_flow_resume_forwards_repo_root(self, captured: dict[str, Any], tmp_path: Path) -> None:
        flow_mod.run_flow_resume(root=tmp_path)
        assert captured["repo_root"] == str(tmp_path)
        assert captured["value"].startswith("resume:")
