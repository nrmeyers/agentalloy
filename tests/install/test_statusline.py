"""Tests for the `statusline` subcommand and its `.claude/settings.json` wiring.

The status line is the *standing state* surface: Claude Code runs it once per
turn and renders the active phase, so the phase stays visible without the proxy
injecting anything. These cover the renderer (phase present / absent / walk-up)
and the wire-time settings merge (claim-when-absent, respect a user's own line,
single combined record with clean-room).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentalloy.install.subcommands.statusline import _find_phase, render_statusline
from agentalloy.install.subcommands.wire import (
    _write_claude_settings,  # pyright: ignore[reportPrivateUsage]
)


def _phase(root: Path, phase: str) -> None:
    d = root / ".agentalloy"
    d.mkdir(parents=True, exist_ok=True)
    (d / "phase").write_text(f"phase: {phase}\nworkflow: sdd-{phase}\n")


class TestRenderStatusline:
    def test_renders_active_phase(self, tmp_path: Path) -> None:
        _phase(tmp_path, "build")
        assert render_statusline(tmp_path) == "⚙ agentalloy ▸ build"

    def test_empty_when_no_phase(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Bound the walk-up at tmp_path so it can't reach a real ancestor phase.
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert render_statusline(tmp_path) == ""

    def test_walks_up_from_subdir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _phase(tmp_path, "spec")
        sub = tmp_path / "src" / "deep"
        sub.mkdir(parents=True)
        assert _find_phase(sub) == "spec"

    def test_stops_at_home_boundary(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Phase lives ABOVE $HOME → not found (the walk stops at the home boundary).
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        (tmp_path / "home").mkdir()
        _phase(tmp_path, "qa")  # at tmp_path, above home
        start = tmp_path / "home" / "proj"
        start.mkdir()
        assert _find_phase(start) is None


class TestStatuslineSettingsWiring:
    def test_creates_settings_with_statusline(self, tmp_path: Path) -> None:
        rec = _write_claude_settings(tmp_path, statusline=True, clean_room=False)
        assert rec is not None
        assert rec.action == "wrote_new_file"
        data = json.loads((tmp_path / ".claude" / "settings.json").read_text())
        assert data["statusLine"]["command"] == "agentalloy statusline"
        assert data["statusLine"]["type"] == "command"

    def test_respects_existing_user_statusline(self, tmp_path: Path) -> None:
        settings = tmp_path / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"statusLine": {"type": "command", "command": "my-own"}}))
        rec = _write_claude_settings(tmp_path, statusline=True, clean_room=False)
        # Nothing to change (user owns the status line) → no record, file untouched.
        assert rec is None
        data = json.loads(settings.read_text())
        assert data["statusLine"]["command"] == "my-own"

    def test_idempotent_when_ours_already_present(self, tmp_path: Path) -> None:
        first = _write_claude_settings(tmp_path, statusline=True, clean_room=False)
        assert first is not None
        # Re-wire: our statusLine is already present → no rewrite.
        again = _write_claude_settings(tmp_path, statusline=True, clean_room=False)
        assert again is None

    def test_statusline_and_clean_room_share_one_record(self, tmp_path: Path) -> None:
        rec = _write_claude_settings(tmp_path, statusline=True, clean_room=True)
        assert rec is not None
        data = json.loads((tmp_path / ".claude" / "settings.json").read_text())
        # Both concerns land in ONE settings.json write (one WireRecord to reverse).
        assert data["statusLine"]["command"] == "agentalloy statusline"
        global_md = str(Path.home() / ".claude" / "CLAUDE.md")
        assert global_md in data["claudeMdExcludes"]

    def test_preserves_unrelated_keys(self, tmp_path: Path) -> None:
        settings = tmp_path / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"model": "opus", "permissions": {"allow": ["Bash"]}}))
        rec = _write_claude_settings(tmp_path, statusline=True, clean_room=False)
        assert rec is not None and rec.action == "injected_block"
        data = json.loads(settings.read_text())
        assert data["model"] == "opus"
        assert data["permissions"] == {"allow": ["Bash"]}
        assert data["statusLine"]["command"] == "agentalloy statusline"

    def test_not_a_json_object_is_left_untouched(self, tmp_path: Path) -> None:
        settings = tmp_path / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text("[1, 2, 3]")  # valid JSON, not an object
        assert _write_claude_settings(tmp_path, statusline=True, clean_room=False) is None
        assert settings.read_text() == "[1, 2, 3]"
