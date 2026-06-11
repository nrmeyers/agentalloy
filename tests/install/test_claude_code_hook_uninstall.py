"""Uninstall coverage for the claude-code hook wiring.

Asserts that after wire-then-uninstall:
- a pre-existing settings.json returns byte-identical to its original,
- a settings.json we created is deleted,
- the installed hook script is removed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentalloy.install.subcommands.uninstall import uninstall
from agentalloy.install.subcommands.wire import apply_hook_wiring
from agentalloy.providers.claude_code.hooks import (
    installed_script_path,
    settings_json_path,
)


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


def test_uninstall_restores_existing_settings_byte_identical(fake_home: Path) -> None:
    settings = settings_json_path()
    settings.parent.mkdir(parents=True, exist_ok=True)
    original = json.dumps({"permissions": {"allow": ["Bash(*)"]}}, indent=2) + "\n"
    settings.write_text(original)

    apply_hook_wiring("claude-code", port=7070, root=fake_home)
    assert settings.read_text() != original  # hooks merged in

    uninstall(remove_user_state=False, remove_env=False, root=fake_home)

    assert settings.exists()
    assert settings.read_text() == original  # byte-identical restore
    assert not installed_script_path().exists()  # script removed


def test_uninstall_deletes_settings_we_created(fake_home: Path) -> None:
    # No pre-existing settings.json — we create it.
    assert not settings_json_path().exists()

    apply_hook_wiring("claude-code", port=7070, root=fake_home)
    assert settings_json_path().exists()

    uninstall(remove_user_state=False, remove_env=False, root=fake_home)

    assert not settings_json_path().exists()  # deleted (we created it)
    assert not installed_script_path().exists()
