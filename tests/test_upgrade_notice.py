"""New-module upgrade notice (spec AC 6, container-module-env-propagation)
plus its code-index-specific supersession.

`_module_notices` diffs MODULE_TOGGLES against the user .env: a default-off
module whose toggle is absent gets exactly one non-interactive line naming
the module and the enable command; a toggle present (either value) means the
user has decided; a default-on module is already running — no notice.

CODE_INDEX_ENABLED is excluded from that generic mechanism (originally the
only toggle it ever fired for) — `_code_index_enable_reminder` supersedes it
with a strictly broader condition (fires whenever off, not just when the key
is absent — every upgrade now unconditionally installs the deps, so a
deliberate opt-out is worth re-surfacing too, not just an unaware one) and
points at the newer `agentalloy code enable` one-command path. Both firing
together would duplicate the line whenever the key happens to be absent.
"""

from pathlib import Path

import pytest

from agentalloy.install import state as install_state
from agentalloy.install.subcommands.upgrade import (
    _code_index_enable_reminder,
    _module_notices,
)


@pytest.fixture
def user_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    cfg_dir = tmp_path / "agentalloy"
    cfg_dir.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    def write(content: str | None) -> None:
        if content is not None:
            (cfg_dir / ".env").write_text(content)

    return write


def test_module_notices_silent_for_code_index_toggle_absent(user_env):
    """Excluded from the generic mechanism — _code_index_enable_reminder owns it."""
    user_env("LOG_LEVEL=info\n")
    assert _module_notices() == []


def test_module_notices_silent_when_env_missing_entirely(user_env):
    user_env(None)
    assert _module_notices() == []


def test_no_notice_for_default_on_module(user_env):
    """COMPOSE_ENABLED (default-on) never fires, absent or not."""
    user_env("CODE_INDEX_ENABLED=1\n")  # the only default-off toggle, present
    assert _module_notices() == []


def test_code_index_reminder_fires_when_toggle_absent(user_env):
    user_env("LOG_LEVEL=info\n")
    reminder = _code_index_enable_reminder(install_state.parse_env_file())
    assert reminder is not None
    assert "agentalloy code enable" in reminder


def test_code_index_reminder_fires_on_deliberate_opt_out(user_env):
    """Unlike the old AC 6 behavior, an explicit CODE_INDEX_ENABLED=0 still
    gets reminded — every upgrade guarantees a working one-command path now,
    so it's worth resurfacing even for a considered opt-out."""
    user_env("CODE_INDEX_ENABLED=0\n")
    assert _code_index_enable_reminder(install_state.parse_env_file()) is not None


def test_code_index_reminder_silent_when_enabled(user_env):
    user_env("CODE_INDEX_ENABLED=1\n")
    assert _code_index_enable_reminder(install_state.parse_env_file()) is None
