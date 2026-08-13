"""Issue #530 — permissions must be re-evaluated and written on every phase
change AND every workflow mode change; posture is a pure function of ``(phase, mode)``.

``agentalloy workflow pause`` used to record ``mode: paused`` correctly and then
silently fail to clear the Tier A deny rules, because
``rewrite_enforcement_posture`` re-derived the pause mode through
``read_pause_state`` — a store-only, in-process-only read (see
``signals.skill_loader``'s docstrings). Called from the CLI, that read finds
no bound store, silently falls back to its documented ``("workflow", None)``
default, and the posture rewriter writes deny rules right back — while
``workflow pause`` reports success, because the mode *write* (a different code
path) succeeded fine. ``workflow resume`` shared the same defect in the opposite,
more dangerous direction: gates failing to RE-ENGAGE.

Two things are asserted here, deliberately kept separate:

* ``TestPostureInvariantMatrix`` — the invariant itself, across
  ``DENIED_PHASES`` and an open phase, in both modes. This is what the issue
  asks for directly, and it is the tripwire for "a future transition forgot
  to rewrite." It runs under the suite's default autouse ``_bound_state_store``
  fixture, so it does NOT reproduce the original bug on its own — the bug only
  manifests when the store is unreachable from the CLI process.
* ``TestPostureSurvivesAnUnreachableStore`` — sabotages the exact seam the bug
  lived in (``_phase_view`` returning ``None``, simulating "no store bound in
  this process") and proves ``workflow pause``/``workflow resume`` still write the
  correct posture, because the fix threads the already-known mode in rather
  than re-deriving it. This is the test that would have failed before the fix
  and is green after it — the CLI-process path, not just the posture builder.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agentalloy.install.subcommands import phase as phase_mod
from agentalloy.install.subcommands import wire_harness
from agentalloy.install.subcommands import workflow as flow_mod
from agentalloy.providers.base import (
    DENIED_PHASES,
    build_claude_code_permissions,
    build_codex_workspace_write,
)

OPEN_PHASES = ("build", "qa", "sdd-fast")
ALL_PHASES = (*sorted(DENIED_PHASES), *OPEN_PHASES)


def _wire_claude_code(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Give *root* a WireRecord for claude-code plus a real settings file.

    Mirrors ``tests/proxy/test_enforcement_posture.py``'s ``_wire_state`` pattern:
    ``load_state`` is monkeypatched rather than going through the real
    install-state file, since only the WireRecord shape matters here.
    """
    settings = root / ".claude" / "settings.local.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text('{"env": {"KEEP": "1"}}\n')
    monkeypatch.setattr(
        wire_harness.install_state,
        "load_state",
        lambda _root: {
            "harness_files_written": [{"repo_root": str(root), "harness": "claude-code"}]
        },
    )
    return settings


def _wire_codex(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Give *root* a WireRecord for codex plus a real ``.codex/config.toml``.

    The task's item 4 asks to audit ``build_codex_workspace_write`` /
    ``_apply_codex_posture`` for the same defect — Tier A includes codex.
    This exercises that path end to end (not just by reading the code), since
    ``build_claude_code_permissions``/``build_codex_workspace_write`` share
    the exact same ``pause_mode`` gate and both flow through
    ``rewrite_enforcement_posture``.
    """
    config = root / ".codex" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text('[other]\nkeep = "1"\n')
    monkeypatch.setattr(
        wire_harness.install_state,
        "load_state",
        lambda _root: {"harness_files_written": [{"repo_root": str(root), "harness": "codex"}]},
    )
    return config


def _permissions(settings_path: Path) -> Any:
    return json.loads(settings_path.read_text(encoding="utf-8"))["permissions"]


def _workspace_write(config_path: Path) -> Any:
    import tomllib

    return tomllib.loads(config_path.read_text()).get("workspace-write")


# ---------------------------------------------------------------------------
# The invariant itself, across the matrix. Green under the default fixture
# (store bound), which does not on its own prove the CLI path is fixed — see
# TestPostureSurvivesAnUnreachableStore below for that.
# ---------------------------------------------------------------------------


class TestPostureInvariantMatrix:
    """Drives ``rewrite_enforcement_posture`` directly — the single evaluation
    point every real call site (``state_router.py``, ``proxy_signal.py``,
    ``workflow.py``) now funnels through with an explicit ``mode``. Note the CLI
    ``phase set`` command itself never rewrote posture (only the HTTP
    phase-advance route, the proxy auto-advance, and workflow pause/resume do) —
    that split is unchanged by this issue and out of its scope.
    """

    @pytest.mark.parametrize("phase", ALL_PHASES)
    @pytest.mark.parametrize("mode", ["workflow", "paused"])
    def test_written_posture_matches_builder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str, mode: str
    ) -> None:
        settings = _wire_claude_code(tmp_path, monkeypatch)
        wire_harness.rewrite_enforcement_posture(tmp_path, phase, mode=mode)

        expected = build_claude_code_permissions(phase, pause_mode=(mode in ("free", "paused")))
        assert _permissions(settings) == expected

    def test_resume_direction_reengages_deny_rules_in_a_denied_phase(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """free -> workflow, landing back in a denied phase, must restore deny rules."""
        settings = _wire_claude_code(tmp_path, monkeypatch)
        phase_mod.run_phase_set("design", root=tmp_path, force=True)
        flow_mod.run_workflow_pause(root=tmp_path)
        assert _permissions(settings) == {"deny": []}  # free: unlocked

        flow_mod.run_workflow_resume(root=tmp_path)
        assert _permissions(settings) == build_claude_code_permissions("design", pause_mode=False)
        assert _permissions(settings)["deny"] != []


# ---------------------------------------------------------------------------
# The regression proof: sabotage the store-unreachable seam and confirm the
# CLI-process path still gets it right, because it no longer depends on that
# seam for the value it already knows.
# ---------------------------------------------------------------------------


class TestPostureSurvivesAnUnreachableStore:
    """Simulates the actual bug condition: no store reachable from this process.

    Patches ``skill_loader._phase_view`` — the exact function
    ``rewrite_enforcement_posture`` used to consult via ``read_flow_state`` —
    to always report "unreachable", the same as a bare CLI invocation with no
    service bound. WireRecord lookups (``install_state.load_state``) are
    file-based and untouched by this, matching the real asymmetry: `flow
    status`/`flow free` write fine (they go through `phase_access`, which
    falls back to HTTP), but the OLD posture rewrite read through a
    store-only, no-HTTP-fallback seam and silently got the wrong answer.
    """

    @pytest.fixture(autouse=True)
    def _sabotage_phase_view(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import agentalloy.signals.skill_loader as skill_loader

        monkeypatch.setattr(skill_loader, "_phase_view", lambda _root: None)

    def test_flow_free_clears_deny_rules_even_though_the_store_read_is_dead(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = _wire_claude_code(tmp_path, monkeypatch)
        phase_mod.run_phase_set("design", root=tmp_path, force=True)
        # Establish the pre-existing deny posture a real phase advance would
        # have left (``phase set`` itself never rewrites posture — see
        # TestPostureInvariantMatrix's note).
        wire_harness.rewrite_enforcement_posture(tmp_path, "design", mode="workflow")
        assert _permissions(settings)["deny"] != []  # sanity: denied before free

        result = flow_mod.run_workflow_pause(root=tmp_path)
        assert result["changed"] is True

        # The old bug: this would still show deny rules here, because the
        # posture rewrite silently fell back to "workflow" through the dead
        # read. The fix passes mode="paused" in directly, bypassing that read.
        assert _permissions(settings) == {"deny": []}

    def test_flow_resume_reengages_deny_rules_even_though_the_store_read_is_dead(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = _wire_claude_code(tmp_path, monkeypatch)
        phase_mod.run_phase_set("spec", root=tmp_path, force=True)
        flow_mod.run_workflow_pause(root=tmp_path)
        assert _permissions(settings) == {"deny": []}

        result = flow_mod.run_workflow_resume(root=tmp_path)
        assert result["changed"] is True

        # The dangerous polarity: gates must RE-ENGAGE here. Before the fix,
        # `flow resume` didn't call the posture rewrite at all.
        assert _permissions(settings)["deny"] != []
        assert _permissions(settings) == build_claude_code_permissions("spec", pause_mode=False)


# ---------------------------------------------------------------------------
# rewrite_enforcement_posture's own explicit-mode contract, and the guard
# against silently coercing an unreachable store to "workflow" when mode is
# omitted (issue item 2).
# ---------------------------------------------------------------------------


class TestRewriteEnforcementPostureExplicitMode:
    def test_explicit_mode_free_bypasses_the_store_read_entirely(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = _wire_claude_code(tmp_path, monkeypatch)
        # No phase row exists at all, so any re-derivation of mode would find
        # nothing. Passing mode="paused" explicitly must still clear deny rules.
        rewritten = wire_harness.rewrite_enforcement_posture(tmp_path, "design", mode="paused")
        assert rewritten == ["claude-code"]
        assert _permissions(settings) == {"deny": []}

    def test_omitted_mode_on_an_unreachable_store_skips_rather_than_guesses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Issue item 2: an unreachable store must not silently write mode=workflow."""
        settings = _wire_claude_code(tmp_path, monkeypatch)
        original = json.loads(settings.read_text(encoding="utf-8"))

        import agentalloy.signals.skill_loader as skill_loader

        monkeypatch.setattr(skill_loader, "_phase_view", lambda _root: None)

        rewritten = wire_harness.rewrite_enforcement_posture(tmp_path, "design")
        assert rewritten == []
        # Untouched — no guessed "workflow" posture was written.
        assert json.loads(settings.read_text(encoding="utf-8")) == original

    def test_verify_enforcement_posture_detects_a_planted_mismatch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = _wire_claude_code(tmp_path, monkeypatch)
        # Plant the exact old bug by hand: mode is free, but deny rules are
        # still on disk (as if the rewrite silently no-op'd).
        settings.write_text(
            json.dumps({"permissions": build_claude_code_permissions("design", pause_mode=False)})
        )
        assert wire_harness.verify_enforcement_posture(tmp_path, "design", "free") == [
            "claude-code"
        ]
        # A matching posture verifies clean.
        settings.write_text(
            json.dumps({"permissions": build_claude_code_permissions("design", pause_mode=True)})
        )
        assert wire_harness.verify_enforcement_posture(tmp_path, "design", "free") == []

    def test_verify_is_silent_on_an_unwired_repo(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(wire_harness.install_state, "load_state", lambda _root: {})
        assert wire_harness.verify_enforcement_posture(tmp_path, "design", "workflow") == []


# ---------------------------------------------------------------------------
# Codex shares the exact same pause_mode gate (build_codex_workspace_write)
# and the exact same rewrite_enforcement_posture call site — issue item 4
# asks this be audited explicitly, not just inferred from the claude-code
# case. build_codex_workspace_write returns {} for an open/free posture, and
# `_apply_codex_posture` DELETES the `workspace-write` key entirely rather
# than writing an empty table — that asymmetry (absent key vs. empty dict) is
# exactly the kind of thing that can silently diverge from the deny-list
# case, so it gets its own round trip.
# ---------------------------------------------------------------------------


class TestCodexPostureRoundTrip:
    def test_matrix_matches_builder(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _wire_codex(tmp_path, monkeypatch)
        for phase in ALL_PHASES:
            for mode in ("workflow", "paused"):
                wire_harness.rewrite_enforcement_posture(tmp_path, phase, mode=mode)
                expected = build_codex_workspace_write(
                    phase, pause_mode=(mode in ("free", "paused"))
                )
                assert _workspace_write(config) == (expected or None)

    def test_free_then_resume_in_a_denied_phase(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _wire_codex(tmp_path, monkeypatch)
        phase_mod.run_phase_set("design", root=tmp_path, force=True)
        wire_harness.rewrite_enforcement_posture(tmp_path, "design", mode="workflow")

        # Narrowed and present under workflow mode in a denied phase.
        ww = _workspace_write(config)
        assert ww is not None
        assert ww["writable_roots"] == build_codex_workspace_write("design")["writable_roots"]

        flow_mod.run_workflow_pause(root=tmp_path)
        # Key is absent entirely under free mode — not an empty table.
        assert _workspace_write(config) is None

        flow_mod.run_workflow_resume(root=tmp_path)
        ww = _workspace_write(config)
        assert ww is not None
        assert ww["writable_roots"] == build_codex_workspace_write("design")["writable_roots"]

    def test_verify_enforcement_posture_covers_codex(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _wire_codex(tmp_path, monkeypatch)
        # Plant the old bug by hand: mode is free, but the narrowed
        # writable_roots are still on disk.
        config.write_text('[workspace-write]\nwritable_roots = ["docs/", ".agentalloy/"]\n')
        assert wire_harness.verify_enforcement_posture(tmp_path, "design", "free") == ["codex"]

        # Correctly cleared (key absent) verifies clean.
        config.write_text('[other]\nkeep = "1"\n')
        assert wire_harness.verify_enforcement_posture(tmp_path, "design", "free") == []
